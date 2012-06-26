# -*- coding: utf-8 -*-
"""
rita_skeeter.py

This is a Python prototype of the C skeeter program.

It will LISTEN to postgres and PUBlish to zeromq
"""
import logging
import os.path
import select
import signal
import sys
from threading import Event
import time

import psycopg2
import psycopg2.extensions
import zmq

class PollError(Exception):
    pass

_psycopg2_states = {
    psycopg2.extensions.POLL_OK     : "ok",
    psycopg2.extensions.POLL_READ   : "read",
    psycopg2.extensions.POLL_WRITE  : "write",
}

_poll_options = {
    "read"  : select.POLLIN  | select.POLLERR,
    "write" : select.POLLOUT | select.POLLERR,
}

def _initialize_logging():
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)-8s %(name)-20s: %(message)s')
    handler.setFormatter(formatter)
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.DEBUG)
    
def _load_config():
    """
    load the configuration dict from .skeeterrc
    """
    numeric_items = set(["hwm",
                         "polling_interval",
                         "notify_check_interval",
                         "heartbeat_interval",
                         "database_retry_delay", ])
    list_items = set(["channels", ])

    config = dict()
    for line in open(os.path.expanduser("~/.skeeterrc"), "r"):
        line = line.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        key, value = line.split("=")
        if key in numeric_items:
            config[key] = int(value)
        elif key in list_items:
            config[key] = value.split(",")
        else:
            config[key] = value

    return config

def _create_signal_handler(halt_event):
    def cb_handler(*_):
        halt_event.set()
    return cb_handler

def _set_signal_handler(halt_event):
    """
    set a signal handler to set halt_event when SIGTERM is raised
    """
    signal.signal(signal.SIGTERM, _create_signal_handler(halt_event))

def _start_listening_channels(config, state):
    """
    Initialize the listening channels
    """
    log = logging.getLogger("_start_listening_channels")

    # this is the point where we're connected to the database
    state["database_connect_time"] = time.time()

    log.info("start listening to {0} channels".format(len(config["channels"])))
    state["cursor"] = state["database_connection"].cursor()
    state["channel_index"] = 0

    state["callback"] = _continue_listening_channels

def _continue_listening_channels(config, state):
    log = logging.getLogger("_continue_listening_channels")
    if state["channel_index"] < len(config["channels"]):
        channel = config["channels"][state["channel_index"]]
        log.info("listening to {0}".format(channel))
        # note: this wants a name, not a quoted SQL string
        state["cursor"].execute("LISTEN {0}".format(channel))
        state["channel_index"] += 1
        state["callback"] = _continue_listening_channels
    else:
        log.debug("committing")
        state["cursor"].execute("commit")
        state["callback"] = _check_for_notifies

def _check_for_notifies(config, state):
    log = logging.getLogger("_check_for_notifies")

    if len(state["database_connection"].notifies) == 0:
        return config["notify_check_interval"]

    log.info("found {0} notifies".format(
        len(state["database_connection"].notifies)))
    while len(state["database_connection"].notifies) > 0:
        notify = state["database_connection"].notifies.pop()
        log.debug("Notify {0} {1} {2} bytes".format(notify.channel,
                                                    notify.pid,
                                                    len(notify.payload)))

        channel = notify.channel
        if notify.payload is None:
            state["pub_socket"].send(channel.encode("utf-8"))
        else:
            state["pub_socket"].send(channel.encode("utf-8"), zmq.SNDMORE)
            state["pub_socket"].send(notify.payload.encode("utf-8"))

        state["callback"] = _check_for_notifies

def _send_heartbeat(config, state):
    log = logging.getLogger("_send_heartbeat")
    state["heartbeat_sequence"] += 1
    message = "sequence={0},database_connect={1}".format(
        state["heartbeat_sequence"], state["database_connect_time"])
    log.debug(message)
    state["pub_socket"].send("heartbeat".encode("utf-8"), zmq.SNDMORE)
    state["pub_socket"].send(message.encode("utf-8"))

def _check_heartbeat_time(config, state):
    current_time = time.time()
    elapsed_time = current_time - state["last_heartbeat_time"]
    if elapsed_time >= config["heartbeat_interval"]:
        _send_heartbeat(config, state)
        state["last_heartbeat_time"] = current_time

def _reset_database_connection(config, state):
    try:
        state["database_connection"].close()
    except Exception:
        pass
    state["database_connect_time"] = None
    state["database_connection"] = _start_database_connection(config)
    state["cursor"] = None
    state["channel_index"] = None
    state["callback"] = _start_listening_channels

def _connection_state_loop(config, state):
    log = logging.getLogger("_connection_state_loop")
    _check_heartbeat_time(config, state)

    psycopg2_state = state["database_connection"].poll()
    connection_state = _psycopg2_states[psycopg2_state]
    while not state["halt_event"].is_set() and  connection_state != "ok":
        log.debug("connection_state = {0}".format(connection_state))
        state["poller"].register(state["database_connection"], 
                                 _poll_options[connection_state])
        
        polling_interval = config["polling_interval"] * 1000.0
        while not state["halt_event"].is_set():
            _check_heartbeat_time(config, state)
            result_list = state["poller"].poll(polling_interval)
            log.debug("result_list {0}".format(result_list))
            if len(result_list) == 0:
                continue
            [(fd, status, )] = result_list
            if status & select.POLLERR != 0:
                raise PollError( "error status from poll {0}".format(status)) 
            break

        psycopg2_state = state["database_connection"].poll()
        connection_state = _psycopg2_states[psycopg2_state]

    if state["halt_event"].is_set():
        return

    state["callback"](config, state)

def _start_database_connection(config):
    return psycopg2.connect(database=config["database"], 
                            async=1)
    
def main():
    """
    main entry point

    returns 0 for success
            1 for failure
    """
    _initialize_logging()
    log = logging.getLogger("main")
    log.info("program starts")

    zeromq_context = zmq.Context()

    config = _load_config()

    state = {
        "halt_event"            : Event(),
        "last_heartbeat_time"   : 0.0,
        "heartbeat_sequence"    : 0,
        "poller"                : select.poll(),
        "database_connect_time" : None,
        "database_connection"   : _start_database_connection(config),
        "pub_socket"            : zeromq_context.socket(zmq.PUB),
        "cursor"                : None,
        "channel_index"         : None,
        "callback"              : _start_listening_channels
    }

    hwm = config.get("hwm")
    if hwm is not None:
        log.info("setting pub_socket HWM to {0}".format(hwm))
        state["pub_socket"].setsockopt(zmq.HWM, hwm)
    state["pub_socket"].setsockopt(zmq.LINGER, 1000)
    log.info("binding pub_socket to {0}".format(config["pub_socket_uri"]))
    state["pub_socket"].bind(config["pub_socket_uri"])

    return_value = 0

    _set_signal_handler(state["halt_event"])
    while not state["halt_event"].is_set():
        try:
            _connection_state_loop(config, state)
        except psycopg2.OperationalError as instance:
            log.error("database error '{0}' retry in {1} seconds".format(
                instance, config["database_retry_delay"]))
            _reset_database_connection(config, state)
            state["halt_event"].wait(config["database_retry_delay"])
        except KeyboardInterrupt:
            log.info("keyboard interrupt")
            state["halt_event"].set()
        except Exception as instance:
            log.exception(str(instance))
            return_value = 1
            state["halt_event"].set()

    log.info("program terminates with return_value {0}".format(return_value))
    state["database_connection"].close()
    state["pub_socket"].close()
    zeromq_context.term()

    return return_value

if __name__ == "__main__":
    sys.exit(main())

