# Home network setup notes

Router: Vireo AX3000, installed in the hallway cupboard.

## Wireless networks

- Main network name (SSID): Bramblenet — WPA3, 5 GHz and 2.4 GHz combined.
- Guest network: Bramblenet-Guest — isolated from the main LAN, password
  on the card stuck inside the cupboard door.

## Getting a device back online

1. Forget the Bramblenet network on the device.
2. Reconnect and enter the passphrase from the password manager entry
   "Home router".
3. If it still fails, power-cycle the Vireo router: unplug for 30 seconds.
4. The admin page is at 192.168.50.1; firmware updates are applied
   automatically on Sunday nights.

## Wired devices

The printer and the media box are plugged into LAN ports 2 and 3 with
fixed addresses .20 and .21.
