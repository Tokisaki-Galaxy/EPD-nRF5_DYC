// Minimal probe build: the panel is driven only through raw commands and the
// OTP dump, so the calendar GUI is compiled out to keep the image small enough
// for the bootloader's DFU cache.
#include "GUI.h"

void DrawGUI(gui_data_t* data, buffer_callback callback, void* callback_data) {
    (void)data;
    (void)callback;
    (void)callback_data;
}
