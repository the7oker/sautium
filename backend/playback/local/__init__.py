"""Local audio output: sounddevice/PortAudio engine (WASAPI / ASIO /
CoreAudio). Available only where the backend runs natively — in a Docker
container PortAudio finds no devices and the output type simply doesn't
exist (see devices.list_devices)."""
