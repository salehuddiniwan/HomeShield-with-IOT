import os

# The tests never open a real serial port, even with an ESP32 plugged in:
# create_app() leaves the USB buzzer link off. tests/test_alarm.py drives it
# with a fake board instead. Nor do they start an MQTT broker: tests/test_iot.py
# drives the hub with a fake one.
os.environ["HOMESHIELD_BUZZER_USB"] = "0"
os.environ["HOMESHIELD_IOT"] = "0"
