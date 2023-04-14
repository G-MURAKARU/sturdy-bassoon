import json
from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

import app.utils as utils
from app import app, bcrypt, db, mqtt, socketio

from .forms import (
    CardRegistrationForm,
    CircuitGenerationForm,
    CircuitSelectionForm,
    LoginForm,
    SentryRegistrationForm,
    UpdateCardForm,
    UpdateSentryForm,
)

from .models import Card, Sentry, Shift, Supervisor
from .mqtts import ALARM, ALERTS, CHKS_OVERDUE, CONNECTED, DONE, SHIFT_ON_OFF

# flag indicating whether the shift is ongoing or not
SHIFT_STATUS = False
# stores the database ID of the circuit currently being monitored
CURRENT_CIRCUIT = None
# stores the generated circuit currently being monitored
SENTRY_CIRCUIT = None
# flag indicating whether the current shift has been completed, set if circuit_handler says so
CIRCUIT_COMPLETED = False
# stores the path patrol frequency list ('paths' variable in 'generate_route' function in 'utils.py')
PATHS = None
# stores the start time of the generated circuit shift (from the DB)
START = 0
# stores the end time of the generated circuit shift (from the DB)
END = 0
# if any alarms are raised or exist in the circuit's DB entry, the times at which they are raised are stored here
# useful for logging purposes
ALARMS = None
# flag indicating whether the alarm has been raised or not
ALARM_TRIGGERED = False
# stores info of the current sentry to be scanned
CURRENT_SCANNED_SENTRY = None

# CONNECTION FLAGS
# these will display connected (green) or disconnected (red) on the homepage
# if supervisor is logged in

# web app connected to broker
APP_CONNECTED = False
# circuit handler connected to broker
HANDLER_CONNECTED = False
# checkpoint A connected to broker
CHK_A_CONNECTED = False
# checkpoint B connected to broker
CHK_B_CONNECTED = False
# checkpoint C connected to broker
CHK_C_CONNECTED = False
# checkpoint D connected to broker
CHK_D_CONNECTED = False


@app.route("/", methods=["GET", "POST"])
@app.route("/home", methods=["GET", "POST"])
def home():
    """
    handles all logic related to the home route, primarily the login form
    """

    form = LoginForm()

    if form.validate_on_submit():
        # check if supervisor entered the correct credentials
        supervisor = Supervisor.query.filter_by(email=form.email.data).first()

        # if so: (NB: password was encrypted with flask_bcrypt before storing, therefore decrypted similarly)
        if supervisor and bcrypt.check_password_hash(supervisor.password, form.password.data):
            login_user(supervisor, remember=form.remember.data)
            flash("Login successful.", "success")

            # redirects supervisor to page they were initialy trying to access if any, else home page
            return (
                redirect(next_page)
                if (next_page := request.args.get("next"))
                else redirect(url_for("home"))
            )
        # if not:
        else:
            flash("Wrong credentials.", "danger")

    # pass variables to the HTML template
    return render_template(
        "home.html",
        title="Home",
        form=form,
        app_connected=APP_CONNECTED,
        handler_connected=HANDLER_CONNECTED,
        chkA_connected=CHK_A_CONNECTED,
        chkB_connected=CHK_B_CONNECTED,
        chkC_connected=CHK_C_CONNECTED,
        chkD_connected=CHK_D_CONNECTED,
        alarm_triggered=ALARM_TRIGGERED,
    )


@app.route("/circuit/create", methods=["GET", "POST"])
@login_required  # ensures that supervisor is logged in to access
def create_route():
    """
    handles the logic of the webpage responsible for displaying the interface to generate a route
    """

    form = CircuitGenerationForm()

    if form.validate_on_submit():
        sentries = form.shift_sentries.data
        cards = form.shift_cards.data
        # number of assigned sentries and cards must be equal, flash message and reload page
        if len(cards) != len(sentries):
            flash(
                "Number of selected sentries and cards must be equal.",
                "danger",
            )
        else:
            # derive data to send to generate_circuit function
            date = form.shift_date.data
            time = form.start.data
            assignments = [
                (sentries[x].full_name, cards[x].alias, cards[x].rfid_id) for x in range(len(cards))
            ]
            hours = int(form.shift_dur_hour.data)
            minutes = int(form.shift_dur_min.data)

            # retrieve start time, end time, path patrol frequencies and full circuit from generate_circuit output
            start, end, paths, circuit = utils.generate_circuit(
                sentries=assignments,
                start_date=date,
                start_time=time,
                shift_dur_hour=hours,
                shift_dur_min=minutes,
            )

            # create a database entry for the generated circuit
            created_route = Shift(
                shift_start=start,
                shift_end=end,
                sentries=assignments,
                circuit=circuit,
                path_freqs=paths,
            )

            # push it to the database
            db.session.add(created_route)
            db.session.commit()

            # flash success message and redirect to view/monitor the route
            flash("Circuit generated.", "success")
            return redirect(url_for("view_current_route"))

    return render_template("generate-route.html", title="Generate Route", form=form)


@app.template_filter("readable")
def derive_date_time(epoch: int):
    """
    converts an epoch timestamp into a readable format with both date and time
    """

    return datetime.fromtimestamp(epoch).strftime("%d/%m/%Y, %H:%M:%S")


@app.template_filter("readable_day")
def derive_date(epoch: int):
    """
    converts an epoch timestamp into a readable format with just the date
    """

    return datetime.fromtimestamp(epoch).strftime("%d/%m/%Y")


@app.template_filter("readable_time")
def derive_time(epoch: int):
    """
    converts an epoch timestamp into a readable format with just the time
    """

    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


@app.route("/circuit/view")
@login_required  # ensures that supervisor is logged in to access
def view_current_route():
    """
    renders the webpage for viewing the current circuit being monitored
    """

    return render_template(
        "view-current-circuit.html",
        sentries=SENTRY_CIRCUIT,
        title="Current Route",
        index=0,
        paths=PATHS,
        start=START,
        end=END,
        completed=CIRCUIT_COMPLETED,
    )


@app.route("/circuit/logs")
@login_required  # ensures that supervisor is logged in to access
def view_all_circuits():
    """
    renders the webpage for viewing all saved circuits
    """

    # query all saved shifts/circuits from the database
    shifts = Shift.query.all()
    return render_template("view-all-circuits.html", circuits=shifts)


@app.route("/circuit/logs/<int:shift_id>")
@login_required  # ensures that supervisor is logged in to access
def view_one_circuit(shift_id):
    """
    renders the webpage for viewing a single saved circuit, similar to view_current_route
    """

    # query a specific shift, filtered using the shift's database ID
    shift = Shift.query.get_or_404(shift_id)
    return render_template(
        "view-circuit.html",
        shift_id=shift.id,
        sentries=shift.circuit,
        title="Previous Route",
        index=0,
        paths=shift.path_freqs,
        start=shift.shift_start,
        end=shift.shift_end,
        completed=shift.completed,
    )


@app.route("/sentries/view")
@login_required  # ensures that supervisor is logged in to access
def view_all_sentries():
    """
    renders the webpage for viewing all registered sentries
    """

    # query all registered sentries from the database
    sentries = Sentry.query.all()
    return render_template("view-all-sentries.html", sentries=sentries)


@app.route("/cards/view")
@login_required  # ensures that supervisor is logged in to access
def view_all_cards():
    """
    renders the webpage for viewing all registered RFID cards
    """

    # query all registered RFID cards from the database
    cards = Card.query.all()
    return render_template("view-all-cards.html", cards=cards)


@app.route("/circuit/select", methods=["GET", "POST"])
@login_required  # ensures that supervisor is logged in to access
def select_circuit():
    """
    handles the logic for webpage displaying interface to select a circuit to monitor
    """

    form = CircuitSelectionForm()

    if form.validate_on_submit():
        # set all relevant information to the defined global variables for accessibility in different functions
        circuit = form.circuit.data

        global SHIFT_STATUS
        SHIFT_STATUS = True
        global CURRENT_CIRCUIT
        CURRENT_CIRCUIT = circuit.id
        global SENTRY_CIRCUIT
        SENTRY_CIRCUIT = circuit.circuit
        global PATHS
        PATHS = circuit.path_freqs
        global START
        START = circuit.shift_start
        global END
        END = circuit.shift_end
        global CIRCUIT_COMPLETED
        CIRCUIT_COMPLETED = circuit.completed
        global ALARMS
        ALARMS = circuit.alarms

        # publish to circuit handler and checkpoints that shift is now being monitored
        mqtt.publish(topic=SHIFT_ON_OFF, payload="ON")

        flash("Shift set!", "success")
        return redirect(url_for("view_current_route"))

    return render_template(
        "select-circuit.html",
        title="Select Circuit",
        form=form,
    )


@app.route("/circuit/save", methods=["GET", "POST"])
@login_required  # ensures that supervisor is logged in to access
def save_current_circuit():
    """
    saves the current state of the circuit to the database, triggered by `Save Circuit` button on view current route page
    """

    circuit = Shift.query.get_or_404(CURRENT_CIRCUIT)
    circuit.circuit = SENTRY_CIRCUIT
    db.session.commit()
    flash("Current circuit saved.", "success")
    return redirect(url_for("view_current_route"))


@app.route("/circuit/deselect")
@login_required  # ensures that supervisor is logged in to access
def deselect_circuit():
    """
    clears the selected circuit from monitoring by clearing global variables
    """

    global SHIFT_STATUS
    SHIFT_STATUS = False
    global CURRENT_CIRCUIT
    CURRENT_CIRCUIT = None
    global SENTRY_CIRCUIT
    SENTRY_CIRCUIT = None
    global START
    START = 0
    global END
    END = 0

    # publish to circuit handler and checkpoints that shift is no longer being monitored
    mqtt.publish(topic=SHIFT_ON_OFF, payload="OFF")

    flash("Shift Deselected.", "info")
    return redirect(url_for("select_circuit"))


@app.route("/circuit/logs/<int:shift_id>/delete", methods=["POST"])
@login_required  # ensures that supervisor is logged in to access
def delete_circuit(shift_id):
    """
    deletes a saved circuit from the database
    """

    shift = Shift.query.get_or_404(shift_id)
    db.session.delete(shift)
    db.session.commit()
    flash("Shift Deleted.", "danger")
    return redirect(url_for("view_all_circuits"))


@app.route("/sentries/register", methods=["GET", "POST"])
@login_required  # ensures that supervisor is logged in to access
def register_sentry():
    """
    handles logic for webpage providing interface for registering a sentry
    """

    form = SentryRegistrationForm()

    if form.validate_on_submit():
        # create sentry object to save to DB then save
        sentry = Sentry(
            national_id=form.national_id.data,
            full_name=form.full_name.data,
            phone_no=form.phone_no.data,
        )
        db.session.add(sentry)
        db.session.commit()
        flash("Sentry registered successfully.", "success")
        return redirect(url_for("home"))

    return render_template(
        "register-sentry.html",
        title="Register Sentry",
        form=form,
        legend="Register Sentry",
    )


@app.route("/sentries/view/<int:sentry_id>/update", methods=["GET", "POST"])
@login_required  # ensures that supervisor is logged in to access
def update_sentry(sentry_id):
    """
    handles logic for webpage providing interface for updating a sentry's info
    """

    sentry = Sentry.query.get_or_404(sentry_id)

    # obj argument will populate the form with the existing data
    form = UpdateSentryForm(obj=sentry)

    if form.validate_on_submit():
        # save changes to DB
        form.populate_obj(sentry)
        db.session.commit()
        flash("Sentry information updated.", "success")
        return redirect(url_for("home"))

    return render_template(
        "register-sentry.html",
        title="Update Sentry",
        form=form,
        legend="Update Sentry",
    )


@app.route("/cards/register", methods=["GET", "POST"])
@login_required  # ensures that supervisor is logged in to access
def register_card():
    """
    handles logic for webpage providing interface for registering a card
    """

    form = CardRegistrationForm()

    if form.validate_on_submit():
        # create card object to save to DB then save
        card = Card(rfid_id=form.card_id.data, alias=form.alias.data)
        db.session.add(card)
        db.session.commit()
        flash("Card registered successfully.", "success")
        return redirect(url_for("home"))

    return render_template(
        "register-card.html",
        title="Register Card",
        form=form,
        legend="Registed RFID Card",
    )


@app.route("/cards/view/<int:card_id>/update", methods=["GET", "POST"])
@login_required  # ensures that supervisor is logged in to access
def update_card(card_id):
    """
    handles logic for webpage providing interface for updating a card's info
    """

    card = Card.query.get_or_404(card_id)

    # obj argument will populate the form with the existing data
    form = UpdateCardForm(obj=card)

    if form.validate_on_submit():
        # save changes to DB
        form.populate_obj(card)
        db.session.commit()
        flash("Card information updated.", "success")
        return redirect(url_for("home"))

    return render_template(
        "register-card.html",
        title="Update Card",
        form=form,
        legend="Update Card",
    )


@app.route("/logout")
@login_required  # ensures that supervisor is logged in to access
def logout():
    """
    logs out the supervisor from the web app
    """

    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("home"))


@app.route("/sentries/view/<int:sentry_id>/delete", methods=["POST"])
@login_required  # ensures that supervisor is logged in to access
def delete_sentry(sentry_id):
    """
    deletes a saved sentry from the database
    """

    sentry = Sentry.query.get_or_404(sentry_id)
    db.session.delete(sentry)
    db.session.commit()
    flash("Sentry Deleted.", "danger")
    return redirect(url_for("view_all_sentries"))


@app.route("/cards/view/<int:card_id>/delete", methods=["POST"])
@login_required  # ensures that supervisor is logged in to access
def delete_card(card_id):
    """
    deletes a saved card from the database
    """

    card = Card.query.get_or_404(card_id)
    db.session.delete(card)
    db.session.commit()
    flash("Card Deleted.", "danger")
    return redirect(url_for("view_all_cards"))


@mqtt.on_connect()
def on_mqtt_connect(client, userdata, flags, rc):
    """
    callback event handler, called when this client connects to the broker
    """

    global APP_CONNECTED

    # rc = return code (CONNACK). on successful connection, rc = 0
    if rc == 0:
        APP_CONNECTED = True
        for topic in [CHKS_OVERDUE, CONNECTED, ALERTS, DONE]:
            mqtt.subscribe(topic=topic, qos=2)


@mqtt.on_message()
def on_mqtt_message(client, userdata, message):
    """
    callback event handler, called when a message is published on any subscribed topic
    """

    topic = message.topic

    # any topic ending with 'connected' e.g. "sentry-platform/checkpoints/connected"
    if topic.split("/")[-1] == "connected":
        payload: dict = json.loads(message.payload)

        # expected payload (JSON string) of the form
        # {
        #     id: client identifier
        #     connected: true if connected, false id disconnected
        # }

        # goal is to display green if connected, red if disconnected in CONNECTION STATUS area

        client, connected = list(payload.values())

        match client:
            case "circuit-handler":
                global HANDLER_CONNECTED
                HANDLER_CONNECTED = bool(connected)
            case "checkpoint-A":
                global CHK_A_CONNECTED
                CHK_A_CONNECTED = bool(connected)
            case "checkpoint-B":
                global CHK_B_CONNECTED
                CHK_B_CONNECTED = bool(connected)
            case "checkpoint-C":
                global CHK_C_CONNECTED
                CHK_C_CONNECTED = bool(connected)
            case "checkpoint-D":
                global CHK_D_CONNECTED
                CHK_D_CONNECTED = bool(connected)

    elif topic.split("/")[-1] == "overdue-scan":
        payload: dict = json.loads(message.payload)

        # expected payload (JSON string) of the form:
        # {
        #   id: ID of assigned card
        #   checkpoint: checkpoint at which above sentry is expected
        #   time: time at which the sentry is expected (epoch)
        #   checked: whether the sentry has validly checked in or not
        # }

        # goal is to display message like
        # OVERDUE CHECK-IN! Sentry with card ID: {id} expected at checkpoint {checkpoint} at {time}
        # on the frontend
        # this will be in layout.html so the message can be flashed regardless of what webpage the supervisor is on

        id, chk, time, _ = list(payload.values())
        # since time is stored as epoch, convert it to a readable format
        time = datetime.fromtimestamp(time).strftime("%H:%M:%S")

        # HTML message to display
        message = f"<strong>OVERDUE CHECK-IN!</strong> Sentry with card ID: {id.upper()} expected at checkpoint {chk} at {time}."

        # set global alarm triggered flag
        global ALARM_TRIGGERED
        ALARM_TRIGGERED = True
        global ALARMS
        ALARMS.append(datetime.now().strftime("%H:%M:%S"))

        # publish "ON" to raise alarm topic
        mqtt.publish(topic=ALARM, payload="ON", qos=2)

        # send the message to the frontend to be displayed
        socketio.emit("alert-msgs", data={"alert_level": "alert-danger", "message": message})

    elif topic == ALERTS:
        payload: dict = json.loads(message.payload)

        # expected payload (JSON string) of the form:
        # {
        #   valid: valid scan or not (bool)
        #   reason: empty if valid, reason for invalid if invalid
        # }

        valid, reason, id, chk, time = list(payload.values())

        if valid:
            # remove the 'valid' and 'reason' keys from the payload dict, then send to be updated
            # i.e pick last 3 (indices 2, 3 and 4)
            scan_info = dict(list(payload.items())[2:])
            utils.update_circuit(circuits=SENTRY_CIRCUIT, scan_info=scan_info)

            message = f"<strong>SUCCESSFUL CHECK-IN!</strong> Sentry with card ID: {id.upper()} checked in at checkpoint {chk} at {time}."

            # send the message to the frontend
            socketio.emit("alert-msgs", data={"alert_level": "alert-success", "message": message})

        else:
            # if card was not on duty, check if card is in database
            # if so, assume card was stolen
            # if not, unknown card

            # else, show alert message
            if reason == "card not on duty":
                # query all registered RFID cards from the database
                cards = Card.query.all()
                tag = "UNKNOWN CARD" if id not in cards else "STOLEN CARD"
            else:
                tag = reason.upper()

            # raise alarm
            global ALARM_TRIGGERED
            ALARM_TRIGGERED = True
            global ALARMS
            ALARMS.append(datetime.now().strftime("%H:%M:%S"))

            mqtt.publish(topic=ALARM, payload="ON", qos=2)

            # send message to frontend
            message = f"<strong>{tag}!</strong> Sentry with card ID: {id.upper()} checked in at checkpoint {chk} at {time}."
            socketio.emit("alert-msgs", data={"alert_level": "alert-danger", "message": message})

    elif topic == DONE:
        global CIRCUIT_COMPLETED
        CIRCUIT_COMPLETED = True
        global SHIFT_STATUS
        SHIFT_STATUS = False

        # send shift over message to clients
        mqtt.publish(topic=SHIFT_ON_OFF, payload="OFF")

        # send message to frontend
        message = "<strong>CIRCUIT COMPLETE!</strong> Save and exit."
        socketio.emit("alert-msgs", data={"alert_level": "alert-success", "message": message})


@socketio.on("silence-alarm")
def silence_alarm():
    """
    deactivates the alarm if it is activated
    triggered by a socket event from the client, when the 'silence alarm' button is clicked
    """

    print("alarm silenced")
    global ALARM_TRIGGERED
    ALARM_TRIGGERED = False

    # publish "OFF" to raise alarm topic
    mqtt.publish(topic=ALARM, payload="OFF", qos=2)

    return redirect(url_for("home"))
