from pybricks.hubs import InventorHub
from pybricks.parameters import Color

hub = InventorHub()

# Say hello with the status light, then prove we're alive over stdout.
hub.light.on(Color.CYAN)
print("Hello from Pybricks on the Inventor Hub!")
print("Battery voltage:", hub.battery.voltage(), "mV")
print("Hub name:", hub.system.name())
hub.light.on(Color.GREEN)
