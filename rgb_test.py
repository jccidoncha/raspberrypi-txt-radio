import time
from gpiozero import RGBLED, Button

# ==========================================
# GPIO CONFIGURATION (BCM numbering)
# ==========================================

# True  = Common cathode (GND)
# False = Common anode (3.3V)
COMMON_CATHODE = True

# RGB LED configuration
led = RGBLED(
    red=17,
    green=27,
    blue=22,
    active_high=COMMON_CATHODE,
    pwm=False
)

# Push button connected to GPIO23 and GND
button = Button(
    23,
    pull_up=True,
    bounce_time=0.05
)

# Test parameters
LED_TIME = 2
TEST_CYCLES = 3


# ==========================================
# RGB LED TEST FUNCTION
# ==========================================

def test_rgb():

    print("\nStarting RGB LED test...")

    for cycle in range(TEST_CYCLES):

        print(f"\nCycle {cycle + 1}/{TEST_CYCLES}")

        # RED
        print("RED LED ON")
        led.color = (1, 0, 0)
        time.sleep(LED_TIME)

        # GREEN
        print("GREEN LED ON")
        led.color = (0, 1, 0)
        time.sleep(LED_TIME)

        # BLUE
        print("BLUE LED ON")
        led.color = (0, 0, 1)
        time.sleep(LED_TIME)

        # WHITE (all colors ON)
        print("ALL COLORS ON")
        led.color = (1, 1, 1)
        time.sleep(LED_TIME)

        # LED OFF
        print("LED OFF")
        led.off()
        time.sleep(LED_TIME)

    print("\nRGB LED TEST COMPLETED")


# ==========================================
# MAIN PROGRAM
# ==========================================

try:

    led.off()

    print("MTP - RGB LED and Button Test")
    print("Press the button to start.")

    while True:

        # Wait until button is pressed
        button.wait_for_press()

        # Wait until button is released
        button.wait_for_release()

        # Run RGB LED test
        test_rgb()

        # Return to standby
        led.off()

        print("\nReady for another test.")
        print("Press the button to start.")


except KeyboardInterrupt:

    print("\nProgram stopped.")

finally:

    led.off()
    led.close()
    button.close()
