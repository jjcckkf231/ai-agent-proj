"""Enable a dedicated USB serial data port for the desktop agent."""

import usb_cdc

usb_cdc.enable(console=True, data=True)

