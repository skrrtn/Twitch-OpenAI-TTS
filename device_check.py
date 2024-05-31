import sounddevice as sd

def print_output_devices():
    print("Available Output Devices:")
    devices = sd.query_devices()
    for i, device in enumerate(devices):
        if device['max_output_channels'] > 0:
            print(f"{i + 1}. {device['name']}")

if __name__ == "__main__":
    print_output_devices()