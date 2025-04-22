import tkinter as tk
import webbrowser
from tkinter import messagebox
import numpy as np
import serial
import serial.tools.list_ports
from collections import deque
from scipy.signal import iirfilter
import time
import pyqtgraph as pg
from PyQt5.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                            QComboBox, QSpinBox, QPushButton, QWidget, QMainWindow)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon
import sys
import queue
import threading
import subprocess
import os
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

class LiveFilter:
    def process(self, x):
        if np.isnan(x):
            return x
        return self._process(x)

    def __call__(self, x):
        return self.process(x)

    def _process(self, x):
        raise NotImplementedError("Derived class must implement _process")

class LiveLFilter(LiveFilter):
    def __init__(self, b, a):
        self.b = b
        self.a = a
        self._xs = deque([0] * len(b), maxlen=len(b))
        self._ys = deque([0] * (len(a) - 1), maxlen=len(a)-1)

    def _process(self, x):
        self._xs.appendleft(x)
        y = np.dot(self.b, self._xs) - np.dot(self.a[1:], self._ys)
        y = y / self.a[0]
        self._ys.appendleft(y)
        return y

class SerialThread(QThread):
    data_ready = pyqtSignal(str)

    def __init__(self, port, baudrate=115200, timeout=1):
        super().__init__()
        self.ser_port = serial.Serial(port, baudrate=baudrate, timeout=timeout)
        self.running = True

    def run(self):
        while self.running:
            if self.ser_port.is_open:
                try:
                    data = self.ser_port.readline().decode().strip()
                    if data:
                        self.data_ready.emit(data)
                except:
                    continue

    def stop(self):
        self.running = False
        if hasattr(self, 'ser_port') and self.ser_port.is_open:
            self.ser_port.close()
        self.wait()

class SignalVisualizer(QMainWindow):
    def __init__(self, channels=1, data_length=1250, port='COM3', amplitude=1000, 
             baudrate=115200, timeout=1, bandpass_freq=[1, 45], notch_freq=50, sample_rate=250):
        super().__init__()
        self.channels = [f"Channel {i+1}" for i in range(channels)]
        self.num_channels = channels
        self.data_length = data_length
        self.data = [np.zeros(data_length) for _ in range(self.num_channels)]
        self.curves = []
        
        # Serial and filter parameters
        self.com = port
        self.baudrate = baudrate
        self.ser_timeout = timeout
        self.bp = bandpass_freq
        self.notch = notch_freq
        self.fs = sample_rate
        self.mail_gui = None  # Add this line
        
        # Initialize filters and parameters
        self.filters = [self.create_filters() for _ in range(self.num_channels)]
        self.setup_parameters()
        self.init_ui(amplitude)
        
        # Command queue for communication with GUIs
        self.command_queue = queue.Queue()
        
        # Start serial communication
        self.serial_thread = SerialThread(self.com, self.baudrate, self.ser_timeout)
        self.serial_thread.data_ready.connect(self.update)
        self.serial_thread.start()

        # Countdown timer
        self.countdown_timer = QTimer()
        self.countdown_timer.timeout.connect(self.update_countdown)
        self.countdown_remaining = 0
        
        # Initial countdown timer
        self.initial_countdown_timer = QTimer()
        self.initial_countdown_timer.timeout.connect(self.update_initial_countdown)
        self.initial_countdown_remaining = 3
        
        # Store the state during countdown
        self.potential_command = None
        self.potential_value = None
        
        # Current active GUI (launcher or VKB)
        self.active_gui = None
        self.launcher_gui = None
        self.vkb_gui = None
        self.notepad_gui = None
        self.vscode_gui = None

    def setup_parameters(self):
        self.eye_open_min = -100
        self.eye_open_max = 100
        self.eye_blink_positive_min = 120
        self.eye_blink_positive_max = 275
        self.eye_blink_negative_min = -120
        self.eye_blink_negative_max = -220
        self.last_print_time = 0
        self.print_delay = 3
        self.eye_open_duration = 5
        self.last_blink_time = 0
        self.blink_cooldown = 1.0
        self.start_time = time.time()
        self.initial_delay = 5
        self.initial_delay_complete = False
        self.ready_for_operation = False

    def closeEvent(self, event):
        if hasattr(self, 'serial_thread'):
            self.serial_thread.stop()
        super().closeEvent(event)

    def create_filters(self):
        b, a = iirfilter(5, Wn=self.bp, fs=self.fs, btype="bandpass", ftype="butter")
        live_lfilter_bp = LiveLFilter(b, a)
        q, p = iirfilter(5, [self.notch - 1.5, self.notch + 1.5], fs=self.fs, btype="bandstop", ftype="butter")
        live_lfilter_notch = LiveLFilter(q, p)
        return (live_lfilter_bp, live_lfilter_notch)

    def init_ui(self, amplitude):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        # Set window dimensions here
        self.resize(300, 150)  # Width: 800px, Height: 600px
        screen_geometry = QApplication.desktop().screenGeometry()
        
        x_offset = 50  # Move 50px left from the right edge
        y_offset = 50  # Move 50px down from the top
        x_pos = screen_geometry.width() - self.width() - x_offset
        y_pos = y_offset
        self.move(x_pos, y_pos)  # Position window
        
        self.timer_label = QLabel("Timer: Initial 5 second delay...", self)
        self.timer_label.setAlignment(Qt.AlignCenter)
        self.timer_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(self.timer_label)
        
        self.plot_widget = pg.GraphicsLayoutWidget()
        layout.addWidget(self.plot_widget)
        
        for i, channel in enumerate(self.channels):
            p = self.plot_widget.addPlot(title=channel)
            p.showGrid(x=True, y=True)
            p.setYRange(-amplitude, amplitude, padding=0)
            curve = p.plot(pen='g')
            self.curves.append(curve)
            if i < self.num_channels - 1:
                self.plot_widget.nextRow()

    

    def start_countdown(self):
        self.countdown_remaining = 3
        self.countdown_timer.start(1000)
        self.timer_label.setText(f"Timer: {self.countdown_remaining} seconds remaining")

    def update_countdown(self):
        self.countdown_remaining -= 1
        if self.countdown_remaining > 0:
            self.timer_label.setText(f"Timer: {self.countdown_remaining} seconds remaining")
        else:
            self.countdown_timer.stop()
            self.timer_label.setText("Timer: Ready")
    
    def send_command(self, command):
        if self.active_gui == "launcher" and self.launcher_gui:
            if command == "right":
                self.launcher_gui.move_to_next_button()
            elif command == "select":
                self.launcher_gui.select_current_button()
        elif self.active_gui == "vkb" and self.vkb_gui:
            self.command_queue.put((command, None))
        elif self.active_gui == "notepad" and self.notepad_gui:
            self.command_queue.put((command, None))
        elif self.active_gui == "vscode" and self.vscode_gui:
            self.command_queue.put((command, None))
        elif self.active_gui == "mail" and self.mail_gui:
            self.command_queue.put((command, None))

    def update(self, data):
        if not self.initial_delay_complete:
            if time.time() - self.start_time >= self.initial_delay:
                self.initial_delay_complete = True
                self.start_initial_countdown()
                # Initialize counters when countdown starts
                self.null_operation_counter = 0
                self.countdown_signals_skipped = 0
            return

        if data != '\r\n' and data != '' and data != '\n':
            try:
                data_values = [float(value) for value in data.split("\t")]
                if len(data_values) != self.num_channels:
                    raise ValueError("Data length mismatch")

                filtered_values = []
                for i in range(self.num_channels):
                    filtered_value = self.filters[i][1](self.filters[i][0](data_values[i]))
                    filtered_values.append(filtered_value)
                    self.data[i] = np.roll(self.data[i], -1)
                    self.data[i][-1] = filtered_value
                    self.curves[i].setData(self.data[i])

                current_time = time.time()
                
                if not self.ready_for_operation:
                    if self.potential_command is None:
                        if all(self.eye_open_min <= value <= self.eye_open_max for value in filtered_values):
                            if current_time - self.last_print_time >= self.eye_open_duration:
                                trigger_value = max(filtered_values, key=abs)
                                self.potential_command = "select"
                                self.potential_value = trigger_value
                                print(f"Detected during countdown: enter ({trigger_value:.3f}) - Skipping (countdown)")
                                self.countdown_signals_skipped += 1
                                
                        elif any((self.eye_blink_positive_min <= value <= self.eye_blink_positive_max) or
                                (self.eye_blink_negative_min >= value >= self.eye_blink_negative_max) for value in data_values):
                            trigger_value = next(
                                value for value in filtered_values
                                if value > self.eye_open_max or value < self.eye_open_min
                            )
                            self.potential_command = "right"
                            self.potential_value = trigger_value
                            print(f"Detected during countdown: right ({trigger_value:.3f}) - Skipping (countdown)")
                            self.countdown_signals_skipped += 1
                    return
                
                if current_time - self.last_print_time >= self.print_delay:
                    if all(self.eye_open_min <= value <= self.eye_open_max for value in filtered_values):
                        if current_time - self.last_print_time >= self.eye_open_duration:
                            trigger_value = max(filtered_values, key=abs)
                            print(f"enter ({trigger_value:.3f})")
                            with open("enter.txt", "a") as f:
                                f.write(f"enter ({trigger_value:.3f})\n")
                            self.last_print_time = current_time
                            
                            # Skip first two operations (including those during countdown)
                            if self.countdown_signals_skipped + self.null_operation_counter < 2:
                                self.null_operation_counter += 1
                                print(f"Skipping enter operation ({self.countdown_signals_skipped + self.null_operation_counter}/2)")
                            else:
                                self.send_command("select")
                            self.start_countdown()
                            
                    elif any((self.eye_blink_positive_min <= value <= self.eye_blink_positive_max) or
                            (self.eye_blink_negative_min >= value >= self.eye_blink_negative_max) for value in data_values):
                        trigger_value = next(
                            value for value in filtered_values
                            if value > self.eye_open_max or value < self.eye_open_min
                        )
                        print(f"right ({trigger_value:.3f})")
                        with open("right.txt", "a") as f:
                            f.write(f"right ({trigger_value:.3f})\n")
                        self.last_print_time = current_time
                        
                        # Skip first two operations (including those during countdown)
                        if self.countdown_signals_skipped + self.null_operation_counter < 2:
                            self.null_operation_counter += 1
                            print(f"Skipping right operation ({self.countdown_signals_skipped + self.null_operation_counter}/2)")
                        elif current_time - self.last_blink_time >= self.blink_cooldown:
                            self.send_command("right")
                            self.last_blink_time = current_time
                        self.start_countdown()

            except ValueError as e:
                print(f"Error processing data: {e}")

    def start_initial_countdown(self):
        self.initial_countdown_remaining = 3
        self.initial_countdown_timer.start(1000)
        self.timer_label.setText(f"Get ready: {self.initial_countdown_remaining} seconds remaining")
        # Reset counters when initial countdown starts
        self.null_operation_counter = 0
        self.countdown_signals_skipped = 0

    def update_initial_countdown(self):
        self.initial_countdown_remaining -= 1
        if self.initial_countdown_remaining > 0:
            self.timer_label.setText(f"Get ready: {self.initial_countdown_remaining} seconds remaining")
        else:
            self.initial_countdown_timer.stop()
            self.timer_label.setText("Timer: Ready")
            self.ready_for_operation = True
            
            # Don't execute any stored commands during initial countdown
            if hasattr(self, 'potential_command'):
                print(f"Ignoring stored command during countdown: {self.potential_command} ({self.potential_value:.3f})")
                self.potential_command = None
                self.potential_value = None


class VSCodeKeyboard(tk.Tk):
    def __init__(self, visualizer):
        super().__init__()
        self.visualizer = visualizer
        self.visualizer.active_gui = "vscode"
        self.visualizer.vscode_gui = self
        
        # Window setup
        self.title("VSCode Keyboard")
        self.geometry("1500x800")
        self.configure(bg="#1a1a2e")
        
        # Color scheme
        self.colors = {
            'bg': "#1a1a2e",
            'text': "#e6e6e6",
            'button_bg': "#16213e",
            'button_fg': "#e6e6e6",
            'highlight': "#0f3460",
            'action_button': "#e94560",
            'hover': "#533483",
            'cursor': "#FFD700",
            'press_effect': "#00FF00",
            'text_box_bg': "#16213e",
            'text_box_fg': "#e6e6e6"
        }

        # Add key press animation variables
        self.press_animation_duration = 200  # milliseconds
        self.currently_pressed_key = None
        
        # File counter for unique filenames
        self.file_counter = 1
        
        # Create text display frame and text box
        self.setup_text_display()
        
        # Create keyboard layout
        self.setup_keyboard()
        
        # Start command processing
        self.process_commands()
        
        # Bind the close event
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Center window
        self.center_window()

    def setup_text_display(self):
        """Setup the text display area with a text box and scrollbar"""
        # Main container frame
        main_frame = tk.Frame(self, bg=self.colors['bg'])
        main_frame.pack(fill='both', expand=True, padx=20, pady=20)
        
        # Text frame (top 40% of window)
        text_frame = tk.Frame(main_frame, bg=self.colors['bg'])
        text_frame.pack(fill='both', expand=True)
        
        # Text label
        text_label = tk.Label(
            text_frame,
            text="Code Editor:",
            font=("Helvetica", 14),
            bg=self.colors['bg'],
            fg=self.colors['text'],
            anchor='w'
        )
        text_label.pack(fill='x')
        
        # Text box with scrollbar
        text_box_frame = tk.Frame(text_frame, bg=self.colors['bg'])
        text_box_frame.pack(fill='both', expand=True)
        
        # Scrollbar
        scrollbar = tk.Scrollbar(text_box_frame)
        scrollbar.pack(side='right', fill='y')
        
        # Text box
        self.text_box = tk.Text(
            text_box_frame,
            font=("Helvetica", 12),
            bg=self.colors['text_box_bg'],
            fg=self.colors['text_box_fg'],
            insertbackground='white',
            wrap='word',
            yscrollcommand=scrollbar.set,
            height=10,
            padx=10,
            pady=10
        )
        self.text_box.pack(fill='both', expand=True)
        scrollbar.config(command=self.text_box.yview)

    def process_commands(self):
        try:
            while True:
                try:
                    command, data = self.visualizer.command_queue.get_nowait()
                    if command == "right":
                        self.move_right()
                    elif command == "select":
                        self.select_highlighted_key(None)
                except queue.Empty:
                    break
        finally:
            self.after(50, self.process_commands)

    def on_closing(self):
        """Handle window closing event - return to launcher"""
        # Stop any pending after() calls
        self.after_cancel_all()
        
        self.visualizer.active_gui = "launcher"
        self.visualizer.vscode_gui = None  # or notepad_gui/vscode_gui/mail_gui
        if self.visualizer.launcher_gui:
            self.visualizer.launcher_gui.deiconify()
        self.destroy()

    def after_cancel_all(self):
        """Cancel all pending after() calls"""
        for id in self.tk.call('after', 'info'):
            self.after_cancel(id)

    def save_content(self):
        """Save the code content to a file and open in VSCode without notification"""
        content = self.text_box.get("1.0", tk.END).strip()
        if content:
            # Create a timestamp for the filename
            while True:
                filename = f"code_{self.file_counter}.py"
                if not os.path.exists(filename):
                    break
                self.file_counter += 1
            
            try:
                with open(filename, "w") as f:
                    f.write(content)
                
                # Try to open with VSCode silently
                try:
                    if sys.platform == "win32":
                        subprocess.Popen(['code', filename], creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        subprocess.Popen(['code', filename])
                except FileNotFoundError:
                    pass  # Silently fail if VSCode not found
            except Exception:
                pass  # Silently fail if save fails


    def setup_keyboard(self):
        # Keyboard frame (bottom 60% of window)
        keyboard_frame = tk.Frame(self, bg=self.colors['bg'])
        keyboard_frame.pack(fill='both', expand=True, padx=20, pady=(0, 20))
        
        # Keyboard layout rows - Modified to add exit key to all rows except last
        self.rows = [
            ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'space', 'enter', 'delete', 'bg', 'exit'],
            ['j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 'space', 'enter', 'delete', 'bg', 'exit'],
            ['s', 't', 'u', 'v', 'w', 'x', 'y', 'z', '.', 'space', 'enter', 'delete', 'bg', 'exit'],
            ['(', ')', ':', '<', '>', '+', '-', '*', '/', '%', 'enter', 'delete', 'bg', 'exit'],
            ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0', 'enter', 'delete', 'exit']
        ]
        
        self.current_row = 0
        self.current_pos = 0
        self.buttons = []
        
        # Create keyboard buttons
        button_style = {
            'font': ('Helvetica', 12, 'bold'),
            'width': 8,
            'height': 2,
            'bd': 0,
            'relief': 'flat',
            'pady': 5,
            'cursor': 'hand2'
        }
        
        for i, row in enumerate(self.rows):
            frame = tk.Frame(keyboard_frame, bg=self.colors['bg'])
            row_buttons = []
            
            for j, button in enumerate(row):
                if button in ['enter', 'delete', 'space', 'bg', 'exit']:
                    bg_color = self.colors['action_button']
                else:
                    bg_color = self.colors['button_bg']
                
                btn = tk.Button(
                    frame,
                    text=button.upper(),
                    bg=bg_color,
                    fg=self.colors['button_fg'],
                    activebackground=self.colors['hover'],
                    activeforeground=self.colors['text'],
                    command=lambda b=button: self.on_button_click(b),
                    **button_style
                )
                
                btn.grid(row=0, column=j, padx=5, pady=5)
                row_buttons.append(btn)
                
                # Bind press and release events for visual feedback
                btn.bind('<ButtonPress-1>', 
                         lambda e, btn=btn: self.on_key_press_visual(btn))
                btn.bind('<ButtonRelease-1>', 
                        lambda e, btn=btn: self.on_key_release_visual(btn))
                btn.bind('<Enter>', lambda e, btn=btn: self.on_hover(btn, True))
                btn.bind('<Leave>', lambda e, btn=btn: self.on_hover(btn, False))
                
            self.buttons.append(row_buttons)
            frame.pack(pady=5)
        
        # Initialize cursor
        self.cursor = tk.Frame(keyboard_frame, bg=self.colors['cursor'], width=10, height=10)
        self.update_cursor_position()

    def on_key_press_visual(self, button):
        """Visual feedback when a key is pressed"""
        button.config(bg=self.colors['press_effect'])
        if self.currently_pressed_key:
            self.on_key_release_visual(self.currently_pressed_key)
        self.currently_pressed_key = button
        self.after(self.press_animation_duration, 
                  lambda: self.on_key_release_visual(button) 
                  if button == self.currently_pressed_key else None)

    def on_key_release_visual(self, button):
        """Restore key appearance after press"""
        if button in ['enter', 'delete', 'space', 'bg', 'exit']:
            button.config(bg=self.colors['action_button'])
        else:
            button.config(bg=self.colors['button_bg'])
        if button == self.currently_pressed_key:
            self.currently_pressed_key = None

    def select_highlighted_key(self, event):
        """Handle selection of the highlighted key"""
        selected_key = self.rows[self.current_row][self.current_pos].lower()
        button = self.buttons[self.current_row][self.current_pos]
        
        # Show visual feedback
        self.on_key_press_visual(button)
        
        # Handle the key press
        if selected_key == "space":
            self.text_box.insert(tk.END, " ")
        elif selected_key == "enter":
            self.text_box.insert(tk.END, "\n")  # Move to next line
        elif selected_key == "delete":
            # Delete last character from text box
            current_text = self.text_box.get("1.0", tk.END)
            if len(current_text) > 1:
                self.text_box.delete("end-2c")
        elif selected_key == "bg":
            self.move_to_first_key_of_row()
        elif selected_key == "exit":
            self.save_content()  # Save content before closing
            self.on_closing()    # Close the window
        else:
            self.text_box.insert(tk.END, selected_key)
        
        # Auto-scroll to end
        self.text_box.see(tk.END)
        
        self.after(self.press_animation_duration, 
                lambda: self.on_key_release_visual(button))

    def on_button_click(self, button):
        """Handle button clicks (manual input)"""
        button = button.lower()
        if button == "space":
            self.text_box.insert(tk.END, " ")
        elif button == "enter":
            self.text_box.insert(tk.END, "\n")  # Move to next line
        elif button == "delete":
            # Delete last character from text box
            current_text = self.text_box.get("1.0", tk.END)
            if len(current_text) > 1:
                self.text_box.delete("end-2c")
        elif button == "bg":
            self.move_to_first_key_of_row()
        elif button == "exit":
            self.save_content()  # Save content before closing
            self.on_closing()    # Close the window
        else:
            self.text_box.insert(tk.END, button)
        
        # Auto-scroll to end
        self.text_box.see(tk.END)

    def update_cursor_position(self):
        """Update the position of the cursor to highlight the current button"""
        if hasattr(self, 'cursor'):
            self.cursor.place_forget()
        
        if (self.current_row < len(self.buttons) and 
            self.current_pos < len(self.buttons[self.current_row])):
            btn = self.buttons[self.current_row][self.current_pos]
            x = btn.winfo_x() + btn.winfo_width()//2 - 5
            y = btn.winfo_y() + btn.winfo_height() + 5
            self.cursor.place(in_=btn.master, x=x, y=y)

    def move_right(self, event=None):
        current_row_length = len(self.rows[self.current_row])
        
        if self.current_pos < current_row_length - 1:
            self.current_pos += 1
        else:
            if self.current_row < len(self.rows) - 1:
                self.current_row += 1
                self.current_pos = 0
            else:
                self.current_row = 0
                self.current_pos = 0
        
        self.update_button_highlight()
        self.update_cursor_position()

    def update_button_highlight(self):
        for i, row in enumerate(self.buttons):
            for j, button in enumerate(row):
                if self.rows[i][j].lower() in ['enter', 'delete', 'space', 'bg', 'exit']:
                    button.configure(bg=self.colors['action_button'])
                else:
                    button.configure(bg=self.colors['button_bg'])
        
        if (self.current_row < len(self.buttons) and (self.current_pos < len(self.buttons[self.current_row]))):
            self.buttons[self.current_row][self.current_pos].configure(
                bg=self.colors['highlight']
            )

    def center_window(self):
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width - 1500) // 2
        y = (screen_height - 800) // 2
        self.geometry(f"1600x800+{x}+{y}")

    def on_hover(self, button, entering):
        if entering:
            button.configure(bg=self.colors['hover'])
        else:
            self.update_button_highlight()

    def move_to_first_key_of_row(self):
        self.current_pos = 0
        self.update_button_highlight()
        self.update_cursor_position()

class VirtualKeyboard(tk.Tk):
    def __init__(self, visualizer):
        super().__init__()
        self.visualizer = visualizer
        self.visualizer.active_gui = "vkb"
        self.visualizer.vkb_gui = self
        
        # Window setup
        self.title("Virtual Keyboard with Signal Visualization")
        self.geometry("1200x600")
        self.configure(bg="#1a1a2e")
        
        # Color scheme
        self.colors = {
            'bg': "#1a1a2e",
            'text': "#e6e6e6",
            'button_bg': "#16213e",
            'button_fg': "#e6e6e6",
            'highlight': "#0f3460",
            'action_button': "#e94560",
            'hover': "#533483",
            'cursor': "#FFD700",  # Gold color for cursor
            'press_effect': "#00FF00",  # Green color for key press effect
            'text_box_bg': "#16213e",
            'text_box_fg': "#e6e6e6"
        }

        # Add key press animation variables
        self.press_animation_duration = 200  # milliseconds
        self.currently_pressed_key = None
        
        # Create text display frame and text box
        self.setup_text_display()
        
        # Create keyboard layout
        self.setup_keyboard()
        
        # Start command processing
        self.process_commands()
        
        # Bind the close event
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_text_display(self):
        """Setup the text display area with a text box and scrollbar"""
        # Main text frame
        text_frame = tk.Frame(self, bg=self.colors['bg'], height=120)
        text_frame.pack(fill='x', pady=(20, 10), padx=20)
        
        # Text label
        text_label = tk.Label(
            text_frame,
            text="Text Input:",
            font=("Helvetica", 14),
            bg=self.colors['bg'],
            fg=self.colors['text'],
            anchor='w'
        )
        text_label.pack(fill='x')
        
        # Text box with scrollbar
        text_box_frame = tk.Frame(text_frame, bg=self.colors['bg'])
        text_box_frame.pack(fill='both', expand=True)
        
        # Scrollbar
        scrollbar = tk.Scrollbar(text_box_frame)
        scrollbar.pack(side='right', fill='y')
        
        # Text box
        self.text_box = tk.Text(
            text_box_frame,
            font=("Helvetica", 12),
            bg=self.colors['text_box_bg'],
            fg=self.colors['text_box_fg'],
            insertbackground='white',
            wrap='word',
            yscrollcommand=scrollbar.set,
            height=5,
            padx=10,
            pady=10
        )
        self.text_box.pack(fill='both', expand=True)
        scrollbar.config(command=self.text_box.yview)
        
        # Initialize text variable for display label (kept for backward compatibility)
        self.text_var = tk.StringVar()
        self.text_var.set("")
        
        # Display label (kept for backward compatibility)
        self.text_display = tk.Label(
            text_frame,
            textvariable=self.text_var,
            font=("Helvetica", 12),
            bg=self.colors['bg'],
            fg=self.colors['text'],
            wraplength=1100,
            justify='left'
        )

    def process_commands(self):
        try:
            while True:
                try:
                    command, data = self.visualizer.command_queue.get_nowait()
                    if command == "right":
                        self.move_right()
                    elif command == "select":
                        self.select_highlighted_key(None)
                except queue.Empty:
                    break
        finally:
            self.after(50, self.process_commands)

    def on_closing(self):
        """Handle window closing event - return to launcher"""
        # Stop any pending after() calls
        self.after_cancel_all()
        
        self.visualizer.active_gui = "launcher"
        self.visualizer.vkb_gui = None  # or notepad_gui/vscode_gui/mail_gui
        if self.visualizer.launcher_gui:
            self.visualizer.launcher_gui.deiconify()
        self.destroy()

    def after_cancel_all(self):
        """Cancel all pending after() calls"""
        for id in self.tk.call('after', 'info'):
            self.after_cancel(id)

    def setup_keyboard(self):
        # Main container frame
        main_frame = tk.Frame(self, bg=self.colors['bg'])
        main_frame.pack(fill='both', expand=True)
        
        # Keyboard frame (bottom 80% of window)
        keyboard_frame = tk.Frame(main_frame, bg=self.colors['bg'])
        keyboard_frame.pack(fill='both', expand=True)
        
        # Keyboard layout rows - Modified to add exit key to all rows except last
        self.row1 = ['a', 'b', 'c', 'd', 'e', 'f', 'space', 'enter', 'delete', 'bg', 'exit']
        self.row2 = ['g', 'h', 'i', 'j', 'k', 'space', 'enter', 'delete', 'bg', 'exit']
        self.row3 = ['l', 'm', 'n', 'o', 'p', 'space', 'enter', 'delete', 'bg', 'exit']
        self.row4 = ['q', 'r', 's', 't', 'u', 'space', 'enter', 'delete', 'bg', 'exit']
        self.row5 = ['v', 'w', 'x', 'y', 'z', 'space', 'enter', 'delete', 'bg', 'exit']
        
        self.rows = [self.row1, self.row2, self.row3, self.row4, self.row5]
        
        self.current_row = 0
        self.current_pos = 0
        self.buttons = []
        
        # Create keyboard buttons
        self.create_keyboard(keyboard_frame)
        
        # Initialize cursor
        self.cursor = tk.Frame(keyboard_frame, bg=self.colors['cursor'], width=10, height=10)
        self.update_cursor_position()
        
        # Center window on screen
        self.center_window()

    def create_keyboard(self, parent_frame):
        button_style = {
            'font': ('Helvetica', 12, 'bold'),
            'width': 8,
            'height': 2,
            'bd': 0,
            'relief': 'flat',
            'pady': 5,
            'cursor': 'hand2'
        }
        
        for i, row in enumerate(self.rows):
            frame = tk.Frame(parent_frame, bg=self.colors['bg'])
            row_buttons = []
            
            for j, button in enumerate(row):
                if button in ['enter', 'delete', 'space', 'bg', 'exit']:
                    bg_color = self.colors['action_button']
                else:
                    bg_color = self.colors['button_bg']
                
                btn = tk.Button(
                    frame,
                    text=button.upper(),
                    bg=bg_color,
                    fg=self.colors['button_fg'],
                    activebackground=self.colors['hover'],
                    activeforeground=self.colors['text'],
                    command=lambda b=button: self.on_button_click(b),
                    **button_style
                )
                
                btn.grid(row=0, column=j, padx=5, pady=5)
                row_buttons.append(btn)
                
                # Bind press and release events for visual feedback
                btn.bind('<ButtonPress-1>', 
                         lambda e, btn=btn: self.on_key_press_visual(btn))
                btn.bind('<ButtonRelease-1>', 
                        lambda e, btn=btn: self.on_key_release_visual(btn))
                btn.bind('<Enter>', lambda e, btn=btn: self.on_hover(btn, True))
                btn.bind('<Leave>', lambda e, btn=btn: self.on_hover(btn, False))
                
            self.buttons.append(row_buttons)
            frame.pack(pady=5)

    def on_key_press_visual(self, button):
        """Visual feedback when a key is pressed"""
        original_color = button.cget('bg')
        button.config(bg=self.colors['press_effect'])
        if self.currently_pressed_key:
            self.on_key_release_visual(self.currently_pressed_key)
        self.currently_pressed_key = button
        self.after(self.press_animation_duration, 
                  lambda: self.on_key_release_visual(button) 
                  if button == self.currently_pressed_key else None)

    def on_key_release_visual(self, button):
        """Restore key appearance after press"""
        if button in ['enter', 'delete', 'space', 'bg', 'exit']:
            button.config(bg=self.colors['action_button'])
        else:
            button.config(bg=self.colors['button_bg'])
        if button == self.currently_pressed_key:
            self.currently_pressed_key = None

    def select_highlighted_key(self, event):
        """Enhanced to show visual feedback when key is selected"""
        selected_key = self.rows[self.current_row][self.current_pos].lower()
        button = self.buttons[self.current_row][self.current_pos]
        
        # Show visual feedback
        self.on_key_press_visual(button)
        
        # Handle the key press
        if selected_key == "space":
            self.text_box.insert(tk.END, " ")
            self.text_var.set(self.text_var.get() + " ")
        elif selected_key == "enter":
            current_text = self.text_box.get("1.0", tk.END).strip()
            self.perform_web_search(current_text)
        elif selected_key == "delete":
            # Delete last character from text box
            current_text = self.text_box.get("1.0", tk.END)
            if len(current_text) > 1:  # Always has at least '\n'
                self.text_box.delete("end-2c")
            # Also update text_var for backward compatibility
            current_var_text = self.text_var.get()
            if current_var_text:
                self.text_var.set(current_var_text[:-1])
        elif selected_key == "bg":
            self.move_to_first_key_of_row()
        elif selected_key == "exit":
            self.on_closing()  # Exit the keyboard
        else:
            self.text_box.insert(tk.END, selected_key)
            self.text_var.set(self.text_var.get() + selected_key)
        
        # Auto-scroll to end
        self.text_box.see(tk.END)
        
        self.after(self.press_animation_duration, 
                lambda: self.on_key_release_visual(button))

    def update_cursor_position(self):
        """Update the position of the cursor to highlight the current button"""
        if hasattr(self, 'cursor'):
            self.cursor.place_forget()
        
        if (self.current_row < len(self.buttons) and 
            self.current_pos < len(self.buttons[self.current_row])):
            btn = self.buttons[self.current_row][self.current_pos]
            x = btn.winfo_x() + btn.winfo_width()//2 - 5
            y = btn.winfo_y() + btn.winfo_height() + 5
            self.cursor.place(in_=btn.master, x=x, y=y)

    def move_right(self, event=None):
        current_row_length = len(self.rows[self.current_row])
        
        if self.current_pos < current_row_length - 1:
            self.current_pos += 1
        else:
            # Move to next row
            self.current_row = (self.current_row + 1) % len(self.rows)
            self.current_pos = 0
        
        self.update_button_highlight()
        self.update_cursor_position()

    def update_button_highlight(self):
        for i, row in enumerate(self.buttons):
            for j, button in enumerate(row):
                if self.rows[i][j].lower() in ['enter', 'delete', 'space', 'bg', 'exit']:
                    button.configure(bg=self.colors['action_button'])
                else:
                    button.configure(bg=self.colors['button_bg'])
        
        if (self.current_row < len(self.buttons) and (self.current_pos < len(self.buttons[self.current_row]))):
            self.buttons[self.current_row][self.current_pos].configure(
                bg=self.colors['highlight']
            )

    def on_button_click(self, button):
        """Handle button clicks (manual input)"""
        button = button.lower()
        if button == "space":
            self.text_box.insert(tk.END, " ")
            self.text_var.set(self.text_var.get() + " ")
        elif button == "enter":
            current_text = self.text_box.get("1.0", tk.END).strip()
            self.perform_web_search(current_text)
        elif button == "delete":
            # Delete last character from text box
            current_text = self.text_box.get("1.0", tk.END)
            if len(current_text) > 1:  # Always has at least '\n'
                self.text_box.delete("end-2c")
            # Also update text_var for backward compatibility
            current_var_text = self.text_var.get()
            if current_var_text:
                self.text_var.set(current_var_text[:-1])
        elif button == "bg":
            self.move_to_first_key_of_row()
        elif button == "exit":
            self.on_closing()  # Exit the keyboard
        else:
            self.text_box.insert(tk.END, button)
            self.text_var.set(self.text_var.get() + button)
        
        # Auto-scroll to end
        self.text_box.see(tk.END)


    def center_window(self):
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width - 1200) // 2
        y = (screen_height - 800) // 2
        self.geometry(f"1250x700+{x}+{y}")

    def on_hover(self, button, entering):
        if entering:
            button.configure(bg=self.colors['hover'])
        else:
            self.update_button_highlight()

    def move_to_first_key_of_row(self):
        self.current_pos = 0
        self.update_button_highlight()
        self.update_cursor_position()

    def perform_web_search(self, search_term):
        base_url = "https://www.google.com/search?q="
        url = base_url + search_term.replace(" ", "+")
        webbrowser.open(url)

class NotepadKeyboard(tk.Tk):
    def __init__(self, visualizer):
        super().__init__()
        self.visualizer = visualizer
        self.visualizer.active_gui = "notepad"
        self.visualizer.notepad_gui = self
        
        # Window setup
        self.title("Notepad with Eye-Controlled Keyboard")
        self.geometry("1400x850")
        self.configure(bg="#1a1a2e")
        
        # Color scheme
        self.colors = {
            'bg': "#1a1a2e",
            'text': "#e6e6e6",
            'button_bg': "#16213e",
            'button_fg': "#e6e6e6",
            'highlight': "#0f3460",
            'action_button': "#e94560",
            'hover': "#533483",
            'cursor': "#FFD700",
            'press_effect': "#00FF00",
            'text_box_bg': "#16213e",
            'text_box_fg': "#e6e6e6"
        }

        # Add key press animation variables
        self.press_animation_duration = 200  # milliseconds
        self.currently_pressed_key = None
        
        # Create text display frame and text box
        self.setup_text_display()
        
        # Create keyboard layout - Modified to add exit key to all rows except last
        self.setup_keyboard()
        
        # Start command processing
        self.process_commands()
        
        # Bind the close event
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Center window
        self.center_window()

    def setup_text_display(self):
        """Setup the text display area with a text box and scrollbar"""
        # Main container frame
        main_frame = tk.Frame(self, bg=self.colors['bg'])
        main_frame.pack(fill='both', expand=True, padx=20, pady=20)
        
        # Text frame (top 40% of window)
        text_frame = tk.Frame(main_frame, bg=self.colors['bg'])
        text_frame.pack(fill='both', expand=True)
        
        # Text label
        text_label = tk.Label(
            text_frame,
            text="Notepad:",
            font=("Helvetica", 14),
            bg=self.colors['bg'],
            fg=self.colors['text'],
            anchor='w'
        )
        text_label.pack(fill='x')
        
        # Text box with scrollbar
        text_box_frame = tk.Frame(text_frame, bg=self.colors['bg'])
        text_box_frame.pack(fill='both', expand=True)
        
        # Scrollbar
        scrollbar = tk.Scrollbar(text_box_frame)
        scrollbar.pack(side='right', fill='y')
        
        # Text box
        self.text_box = tk.Text(
            text_box_frame,
            font=("Helvetica", 12),
            bg=self.colors['text_box_bg'],
            fg=self.colors['text_box_fg'],
            insertbackground='white',
            wrap='word',
            yscrollcommand=scrollbar.set,
            height=10,
            padx=10,
            pady=10
        )
        self.text_box.pack(fill='both', expand=True)
        scrollbar.config(command=self.text_box.yview)

    def process_commands(self):
        try:
            while True:
                try:
                    command, data = self.visualizer.command_queue.get_nowait()
                    if command == "right":
                        self.move_right()
                    elif command == "select":
                        self.select_highlighted_key(None)
                except queue.Empty:
                    break
        finally:
            self.after(50, self.process_commands)

    def on_closing(self):
        """Handle window closing event - save content and return to launcher"""
        self.save_content()  # Save content when closing
        # Restore the launcher GUI when exiting
        self.visualizer.active_gui = "launcher"
        self.visualizer.notepad_gui = None
        if self.visualizer.launcher_gui:
            self.visualizer.launcher_gui.deiconify()
        self.destroy()

    def save_content(self):
        """Save the notepad content to a temporary file"""
        content = self.text_box.get("1.0", tk.END).strip()
        if content:
            # Create a timestamp for the filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"notepad_{timestamp}.txt"
            
            try:
                with open(filename, "w") as f:
                    f.write(content)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save file: {str(e)}")

    def setup_keyboard(self):
        # Keyboard frame (bottom 60% of window)
        keyboard_frame = tk.Frame(self, bg=self.colors['bg'])
        keyboard_frame.pack(fill='both', expand=True, padx=20, pady=(0, 20))
        
        # Keyboard layout rows - Modified to add exit key to all rows except last
        self.rows = [
            ['a', 'b', 'c', 'd', 'e', 'f', '1', '2', 'space', 'enter', 'delete', 'bg', 'exit'],
            ['g', 'h', 'i', 'j', 'k', '3', '4', 'space', 'enter', 'delete', 'bg', 'exit'],
            ['l', 'm', 'n', 'o', 'p', '5', '6', 'space', 'enter', 'delete', 'bg', 'exit'],
            ['q', 'r', 's', 't', 'u', '7', '8', 'space', 'enter', 'delete', 'bg', 'exit'],
            ['v', 'w', 'x', 'y', 'z', '9', '0', 'space', 'enter', 'delete', 'exit']
        ]
        
        self.current_row = 0
        self.current_pos = 0
        self.buttons = []
        
        # Create keyboard buttons
        button_style = {
            'font': ('Helvetica', 12, 'bold'),
            'width': 8,
            'height': 2,
            'bd': 0,
            'relief': 'flat',
            'pady': 5,
            'cursor': 'hand2'
        }
        
        for i, row in enumerate(self.rows):
            frame = tk.Frame(keyboard_frame, bg=self.colors['bg'])
            row_buttons = []
            
            for j, button in enumerate(row):
                if button in ['enter', 'delete', 'space', 'bg', 'exit']:
                    bg_color = self.colors['action_button']
                else:
                    bg_color = self.colors['button_bg']
                
                btn = tk.Button(
                    frame,
                    text=button.upper(),
                    bg=bg_color,
                    fg=self.colors['button_fg'],
                    activebackground=self.colors['hover'],
                    activeforeground=self.colors['text'],
                    command=lambda b=button: self.on_button_click(b),
                    **button_style
                )
                
                btn.grid(row=0, column=j, padx=5, pady=5)
                row_buttons.append(btn)
                
                # Bind press and release events for visual feedback
                btn.bind('<ButtonPress-1>', 
                         lambda e, btn=btn: self.on_key_press_visual(btn))
                btn.bind('<ButtonRelease-1>', 
                        lambda e, btn=btn: self.on_key_release_visual(btn))
                btn.bind('<Enter>', lambda e, btn=btn: self.on_hover(btn, True))
                btn.bind('<Leave>', lambda e, btn=btn: self.on_hover(btn, False))
                
            self.buttons.append(row_buttons)
            frame.pack(pady=5)
        
        # Initialize cursor
        self.cursor = tk.Frame(keyboard_frame, bg=self.colors['cursor'], width=10, height=10)
        self.update_cursor_position()

    def on_key_press_visual(self, button):
        """Visual feedback when a key is pressed"""
        button.config(bg=self.colors['press_effect'])
        if self.currently_pressed_key:
            self.on_key_release_visual(self.currently_pressed_key)
        self.currently_pressed_key = button
        self.after(self.press_animation_duration, 
                  lambda: self.on_key_release_visual(button) 
                  if button == self.currently_pressed_key else None)

    def on_key_release_visual(self, button):
        """Restore key appearance after press"""
        if button in ['enter', 'delete', 'space', 'bg', 'exit']:
            button.config(bg=self.colors['action_button'])
        else:
            button.config(bg=self.colors['button_bg'])
        if button == self.currently_pressed_key:
            self.currently_pressed_key = None

    def select_highlighted_key(self, event):
        """Handle selection of the highlighted key"""
        selected_key = self.rows[self.current_row][self.current_pos].lower()
        button = self.buttons[self.current_row][self.current_pos]
        
        # Show visual feedback
        self.on_key_press_visual(button)
        
        # Handle the key press
        if selected_key == "space":
            self.text_box.insert(tk.END, " ")
        elif selected_key == "enter":
            self.text_box.insert(tk.END, "\n")  # Insert newline for enter key
        elif selected_key == "delete":
            # Delete last character from text box
            current_text = self.text_box.get("1.0", tk.END)
            if len(current_text) > 1:
                self.text_box.delete("end-2c")
        elif selected_key == "bg":
            self.move_to_first_key_of_row()
        elif selected_key == "exit":
            self.save_content()  # Save content before exiting
            self.on_closing()  # Exit the notepad
        else:
            self.text_box.insert(tk.END, selected_key)
        
        # Auto-scroll to end
        self.text_box.see(tk.END)
        
        self.after(self.press_animation_duration, 
                lambda: self.on_key_release_visual(button))

    def update_cursor_position(self):
        """Update the position of the cursor to highlight the current button"""
        if hasattr(self, 'cursor'):
            self.cursor.place_forget()
        
        if (self.current_row < len(self.buttons) and 
            self.current_pos < len(self.buttons[self.current_row])):
            btn = self.buttons[self.current_row][self.current_pos]
            x = btn.winfo_x() + btn.winfo_width()//2 - 5
            y = btn.winfo_y() + btn.winfo_height() + 5
            self.cursor.place(in_=btn.master, x=x, y=y)

    def move_right(self, event=None):
        current_row_length = len(self.rows[self.current_row])
        
        if self.current_pos < current_row_length - 1:
            self.current_pos += 1
        else:
            if self.current_row < len(self.rows) - 1:
                self.current_row += 1
                self.current_pos = 0
            else:
                self.current_row = 0
                self.current_pos = 0
        
        self.update_button_highlight()
        self.update_cursor_position()

    def update_button_highlight(self):
        for i, row in enumerate(self.buttons):
            for j, button in enumerate(row):
                if self.rows[i][j] in ['enter', 'delete', 'space', 'bg', 'exit']:
                    button.configure(bg=self.colors['action_button'])
                else:
                    button.configure(bg=self.colors['button_bg'])
        
        if (self.current_row < len(self.buttons) and (self.current_pos < len(self.buttons[self.current_row]))):
            self.buttons[self.current_row][self.current_pos].configure(
                bg=self.colors['highlight']
            )

    def on_button_click(self, button):
        """Handle button clicks (manual input)"""
        button = button.lower()
        if button == "space":
            self.text_box.insert(tk.END, " ")
        elif button == "enter":
            self.text_box.insert(tk.END, "\n")  # Insert newline for enter key
        elif button == "delete":
            # Delete last character from text box
            current_text = self.text_box.get("1.0", tk.END)
            if len(current_text) > 1:
                self.text_box.delete("end-2c")
        elif button == "bg":
            self.move_to_first_key_of_row()
        elif button == "exit":
            self.save_content()  # Save content before exiting
            self.on_closing()  # Exit the notepad
        else:
            self.text_box.insert(tk.END, button)
        
        # Auto-scroll to end
        self.text_box.see(tk.END)

    def center_window(self):
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width - 1400) // 2
        y = (screen_height - 800) // 2
        self.geometry(f"1500x800+{x}+{y}")

    def on_hover(self, button, entering):
        if entering:
            button.configure(bg=self.colors['hover'])
        else:
            self.update_button_highlight()

    def move_to_first_key_of_row(self):
        self.current_pos = 0
        self.update_button_highlight()
        self.update_cursor_position()

class MailKeyboard(tk.Tk):
    def __init__(self, visualizer):
        super().__init__()
        self.visualizer = visualizer
        self.visualizer.active_gui = "mail"
        self.visualizer.mail_gui = self
        
        # Email credentials
        self.my_email = "mirshantm@gmail.com"
        self.password = "rwos jfao vwku sriu"
        
        # Window setup
        self.title("Virtual Keyboard with Email")
        self.geometry("1450x700")
        self.configure(bg="#1a1a2e")
        
        # Color scheme
        self.colors = {
            'bg': "#1a1a2e",
            'text': "#e6e6e6",
            'button_bg': "#16213e",
            'button_fg': "#e6e6e6",
            'highlight': "#0f3460",
            'action_button': "#e94560",
            'hover': "#533483",
            'cursor': "#FFD700",
            'press_effect': "#00FF00",
            'field_bg': "#2a2a4e",
            'active_field': "#3a3a6e"
        }
        
        # Active field tracking
        self.active_field = {"current": "to"}
        
        # Add key press animation variables
        self.press_animation_duration = 200  # milliseconds
        self.currently_pressed_key = None
        
        # Create email fields and keyboard layout
        self.setup_email_fields()
        self.setup_keyboard()
        
        # Start command processing
        self.process_commands()
        
        # Bind the close event
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Center window
        self.center_window()

    def setup_email_fields(self):
        # Create email fields frame
        email_frame = tk.Frame(self, bg=self.colors['bg'], pady=20)
        email_frame.pack(fill='x', padx=20)
        
        # To field
        to_label = tk.Label(email_frame, text="To:", bg=self.colors['bg'], fg=self.colors['text'], font=("Helvetica", 12))
        to_label.grid(row=0, column=0, padx=5, pady=5, sticky='e')
        self.to_entry = tk.Entry(email_frame, bg=self.colors['field_bg'], fg=self.colors['text'], 
                                font=("Helvetica", 12), width=50)
        self.to_entry.grid(row=0, column=1, padx=5, pady=5, sticky='w')
        self.to_entry.bind('<FocusOut>', self.append_gmail_domain)
        
        # Subject field
        subject_label = tk.Label(email_frame, text="Subject:", bg=self.colors['bg'], fg=self.colors['text'], 
                               font=("Helvetica", 12))
        subject_label.grid(row=1, column=0, padx=5, pady=5, sticky='e')
        self.subject_entry = tk.Entry(email_frame, bg=self.colors['field_bg'], fg=self.colors['text'], 
                                    font=("Helvetica", 12), width=50)
        self.subject_entry.grid(row=1, column=1, padx=5, pady=5, sticky='w')
        
        # Body field
        body_label = tk.Label(email_frame, text="Body:", bg=self.colors['bg'], fg=self.colors['text'], 
                            font=("Helvetica", 12))
        body_label.grid(row=2, column=0, padx=5, pady=5, sticky='ne')
        self.body_text = tk.Text(email_frame, bg=self.colors['field_bg'], fg=self.colors['text'], 
                                font=("Helvetica", 12), width=50, height=5)
        self.body_text.grid(row=2, column=1, padx=5, pady=5, sticky='w')
        
        # Set initial active field
        self.to_entry.focus()
        self.update_active_field_colors()

    def append_gmail_domain(self, event=None):
        """Append @gmail.com to the To field if it's not already there"""
        current_text = self.to_entry.get()
        if current_text and not current_text.endswith('@gmail.com'):
            clean_text = current_text.split('@')[0]
            self.to_entry.delete(0, tk.END)
            self.to_entry.insert(0, f"{clean_text}@gmail.com")

    def update_active_field_colors(self):
        # Reset all fields to default color
        self.to_entry.configure(bg=self.colors['field_bg'])
        self.subject_entry.configure(bg=self.colors['field_bg'])
        self.body_text.configure(bg=self.colors['field_bg'])
        
        # Highlight active field
        if self.active_field["current"] == "to":
            self.to_entry.configure(bg=self.colors['active_field'])
        elif self.active_field["current"] == "subject":
            self.subject_entry.configure(bg=self.colors['active_field'])
        elif self.active_field["current"] == "body":
            self.body_text.configure(bg=self.colors['active_field'])

    def setup_keyboard(self):
        # Keyboard layout from mail3.py
        self.rows = [
            ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'space', 'enter', 'delete', "send", 'bg', "Left", "Right"],
            ['j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 'space', 'enter', 'delete', "send", 'bg', "Left", "Right"],
            ['s', 't', 'u', 'v', 'w', 'x', 'y', 'z', '.', '@', 'space', 'enter', 'delete', "send", 'bg', "Left", "Right"],
            ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0', 'enter', 'delete', "send", 'bg'],
            ["Left", "Right", "Send", "bg", "exit"]
        ]
        
        self.current_row = 0
        self.current_pos = 0
        self.buttons = []
        
        # Create keyboard frame
        self.keyboard_frame = tk.Frame(self, bg=self.colors['bg'], pady=20)
        self.keyboard_frame.pack(expand=True)
        
        # Create keyboard buttons with updated style
        button_style = {
            'font': ('Helvetica', 12, 'bold'),
            'width': 6,
            'height': 2,
            'bd': 0,
            'relief': 'flat',
            'pady': 5,
            'cursor': 'hand2'
        }
        
        for i, row in enumerate(self.rows):
            frame = tk.Frame(self.keyboard_frame, bg=self.colors['bg'])
            row_buttons = []
            
            for j, button in enumerate(row):
                if button.lower() in ['enter', 'delete', 'space', 'bg', 'exit', 'send', 'left', 'right']:
                    bg_color = self.colors['action_button']
                else:
                    bg_color = self.colors['button_bg']
                
                btn = tk.Button(
                    frame,
                    text=button.upper(),
                    bg=bg_color,
                    fg=self.colors['button_fg'],
                    activebackground=self.colors['hover'],
                    activeforeground=self.colors['text'],
                    command=lambda b=button: self.on_button_click(b),
                    **button_style
                )
                
                btn.grid(row=0, column=j, padx=2, pady=2)
                row_buttons.append(btn)
                
                btn.bind('<ButtonPress-1>', 
                         lambda e, btn=btn: self.on_key_press_visual(btn))
                btn.bind('<ButtonRelease-1>', 
                        lambda e, btn=btn: self.on_key_release_visual(btn))
                btn.bind('<Enter>', lambda e, btn=btn: self.on_hover(btn, True))
                btn.bind('<Leave>', lambda e, btn=btn: self.on_hover(btn, False))
            
            self.buttons.append(row_buttons)
            frame.pack(pady=2)

        self.update_button_highlight()
        self.update_cursor_position()

    def on_key_press_visual(self, button):
        """Visual feedback when a key is pressed"""
        button.config(bg=self.colors['press_effect'])
        if self.currently_pressed_key:
            self.on_key_release_visual(self.currently_pressed_key)
        self.currently_pressed_key = button
        self.after(self.press_animation_duration, 
                  lambda: self.on_key_release_visual(button) 
                  if button == self.currently_pressed_key else None)

    def on_key_release_visual(self, button):
        """Restore key appearance after press"""
        if button.cget('text').lower() in ['enter', 'delete', 'space', 'bg', 'exit', 'send', 'left', 'right']:
            button.config(bg=self.colors['action_button'])
        else:
            button.config(bg=self.colors['button_bg'])
        if button == self.currently_pressed_key:
            self.currently_pressed_key = None

    def process_commands(self):
        try:
            while True:
                try:
                    command, data = self.visualizer.command_queue.get_nowait()
                    if command == "right":
                        self.move_right()
                    elif command == "select":
                        self.select_highlighted_key(None)
                except queue.Empty:
                    break
        finally:
            self.after(50, self.process_commands)

    def on_closing(self):
        """Handle window closing event - return to launcher"""
        # Stop any pending after() calls
        self.after_cancel_all()
        
        self.visualizer.active_gui = "launcher"
        self.visualizer.mail_gui = None  # or notepad_gui/vscode_gui/mail_gui
        if self.visualizer.launcher_gui:
            self.visualizer.launcher_gui.deiconify()
        self.destroy()

    def after_cancel_all(self):
        """Cancel all pending after() calls"""
        for id in self.tk.call('after', 'info'):
            self.after_cancel(id)

    def navigate_field(self, direction):
        if direction == "Left":
            if self.active_field["current"] == "body":
                self.active_field["current"] = "subject"
                self.subject_entry.focus()
            elif self.active_field["current"] == "subject":
                self.active_field["current"] = "to"
                self.to_entry.focus()
        elif direction == "Right":
            if self.active_field["current"] == "to":
                self.active_field["current"] = "subject"
                self.subject_entry.focus()
            elif self.active_field["current"] == "subject":
                self.active_field["current"] = "body"
                self.body_text.focus()
        
        self.update_active_field_colors()

    def send_email(self):
        try:
            # Ensure the @gmail.com is added before sending
            self.append_gmail_domain()
            
            msg = MIMEMultipart()
            msg['From'] = self.my_email
            msg['To'] = self.to_entry.get()
            msg['Subject'] = self.subject_entry.get()
            
            body = self.body_text.get("1.0", tk.END)
            msg.attach(MIMEText(body, 'plain'))
            
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(self.my_email, self.password)
                server.send_message(msg)
        finally:
            # Always close the window and return to launcher
            self.on_closing()

    def select_highlighted_key(self, event):
        selected_key = self.rows[self.current_row][self.current_pos].lower()
        button = self.buttons[self.current_row][self.current_pos]
        
        # Show visual feedback
        self.on_key_press_visual(button)
        
        if selected_key in ["left", "right"]:
            self.navigate_field(selected_key.capitalize())
        elif selected_key == "send":
            self.send_email()  # This will now close the window after sending
        else:
            current_text = ""
            if self.active_field["current"] == "to":
                current_text = self.to_entry.get()
                if selected_key == "space":
                    self.to_entry.insert(tk.END, " ")
                elif selected_key == "delete":
                    self.to_entry.delete(len(current_text)-1, tk.END)
                elif selected_key == "enter":
                    self.navigate_field("Right")
                elif selected_key not in ["bg", "exit"]:
                    self.to_entry.insert(tk.END, selected_key)
            
            elif self.active_field["current"] == "subject":
                current_text = self.subject_entry.get()
                if selected_key == "space":
                    self.subject_entry.insert(tk.END, " ")
                elif selected_key == "delete":
                    self.subject_entry.delete(len(current_text)-1, tk.END)
                elif selected_key == "enter":
                    self.navigate_field("Right")
                elif selected_key not in ["bg", "exit"]:
                    self.subject_entry.insert(tk.END, selected_key)
            
            elif self.active_field["current"] == "body":
                if selected_key == "space":
                    self.body_text.insert(tk.END, " ")
                elif selected_key == "delete":
                    self.body_text.delete("end-2c", tk.END)
                elif selected_key == "enter":
                    self.body_text.insert(tk.END, "\n")
                elif selected_key not in ["bg", "exit"]:
                    self.body_text.insert(tk.END, selected_key)

            if selected_key == "exit":
                self.on_closing()
            elif selected_key == "bg":
                self.move_to_first_key_of_row()
        
        self.after(self.press_animation_duration, 
                lambda: self.on_key_release_visual(button))

    def on_button_click(self, button):
        """Handle manual button clicks"""
        button_lower = button.lower()
        if button_lower in ["left", "right"]:
            self.navigate_field(button_lower.capitalize())
        elif button_lower == "send":
            self.send_email()  # This will now close the window after sending
        else:
            current_text = ""
            if self.active_field["current"] == "to":
                current_text = self.to_entry.get()
                if button_lower == "space":
                    self.to_entry.insert(tk.END, " ")
                elif button_lower == "delete":
                    self.to_entry.delete(len(current_text)-1, tk.END)
                elif button_lower == "enter":
                    self.navigate_field("Right")
                elif button_lower not in ["bg", "exit"]:
                    self.to_entry.insert(tk.END, button_lower)
            
            elif self.active_field["current"] == "subject":
                current_text = self.subject_entry.get()
                if button_lower == "space":
                    self.subject_entry.insert(tk.END, " ")
                elif button_lower == "delete":
                    self.subject_entry.delete(len(current_text)-1, tk.END)
                elif button_lower == "enter":
                    self.navigate_field("Right")
                elif button_lower not in ["bg", "exit"]:
                    self.subject_entry.insert(tk.END, button_lower)
            
            elif self.active_field["current"] == "body":
                if button_lower == "space":
                    self.body_text.insert(tk.END, " ")
                elif button_lower == "delete":
                    self.body_text.delete("end-2c", tk.END)
                elif button_lower == "enter":
                    self.body_text.insert(tk.END, "\n")
                elif button_lower not in ["bg", "exit"]:
                    self.body_text.insert(tk.END, button_lower)

            if button_lower == "exit":
                self.on_closing()
            elif button_lower == "bg":
                self.move_to_first_key_of_row()

    def update_cursor_position(self):
        """Update the position of the cursor to highlight the current button"""
        if hasattr(self, 'cursor'):
            self.cursor.place_forget()
        
        if (self.current_row < len(self.buttons) and (self.current_pos < len(self.buttons[self.current_row]))):
            btn = self.buttons[self.current_row][self.current_pos]
            x = btn.winfo_x() + btn.winfo_width()//2 - 5
            y = btn.winfo_y() + btn.winfo_height() + 5
            self.cursor = tk.Frame(btn.master, bg=self.colors['cursor'], width=10, height=10)
            self.cursor.place(in_=btn.master, x=x, y=y)

    def move_right(self, event=None):
        current_row_length = len(self.rows[self.current_row])
        
        if self.current_pos < current_row_length - 1:
            self.current_pos += 1
        else:
            if self.current_row < len(self.rows) - 1:
                self.current_row += 1
                self.current_pos = 0
            else:
                self.current_row = 0
                self.current_pos = 0
        
        self.update_button_highlight()
        self.update_cursor_position()

    def update_button_highlight(self):
        for i, row in enumerate(self.buttons):
            for j, button in enumerate(row):
                if self.rows[i][j].lower() in ['enter', 'delete', 'space', 'bg', 'exit', 'send', 'left', 'right']:
                    button.configure(bg=self.colors['action_button'])
                else:
                    button.configure(bg=self.colors['button_bg'])
        
        if (self.current_row < len(self.buttons) and (self.current_pos < len(self.buttons[self.current_row]))):
            self.buttons[self.current_row][self.current_pos].configure(
                bg=self.colors['highlight']
            )


    def center_window(self):
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width - 1450) // 2
        y = (screen_height - 700) // 2
        self.geometry(f"1450x700+{x}+{y}")

    def on_hover(self, button, entering):
        if entering:
            button.configure(bg=self.colors['hover'])
        else:
            self.update_button_highlight()

    def move_to_first_key_of_row(self):
        self.current_pos = 0
        self.update_button_highlight()
        self.update_cursor_position()


class LauncherGUI(tk.Tk):
    def __init__(self, visualizer):
        super().__init__()
        self.visualizer = visualizer
        self.visualizer.active_gui = "launcher"
        self.visualizer.launcher_gui = self
        
        self.title("Eye-Controlled Launcher")
        self.geometry("500x600")
        self.configure(bg="#f0f0f0")
        
        self.buttons = []
        self.current_button_index = 0
        self.setup_gui()
        
        # Add cursor
        self.cursor = tk.Frame(self, bg="#FFD700", width=10, height=10)
        self.update_cursor_position()

    def setup_gui(self):
        main_frame = tk.Frame(self, bg="#f0f0f0", padx=20, pady=20)
        main_frame.pack(expand=True, fill="both")
        
        title_label = tk.Label(
            main_frame,
            text="Eye-Controlled Launcher",
            font=("Helvetica", 16, "bold"),
            bg="#f0f0f0",
            fg="#333333"
        )
        title_label.pack(pady=(0, 20))
        
        button_style = {
            "font": ("Helvetica", 11),
            "width": 25,
            "height": 2,
            "borderwidth": 0,
            "relief": "flat",
            "cursor": "hand2"
        }
        
        buttons_info = [
            ("Google Search", self.launch_google_search, "#4285F4"),
            ("Notepad", self.launch_notepad, "#2196F3"),
            ("Mail", self.launch_mail, "#4CAF50"),
            ("VS Code", self.launch_vscode, "#007ACC"),
            ("Alert", self.send_alert, "#FF9800"),  
            ("Exit", self.exit_program, "#FF5252")
        ]
        
        for text, command, color in buttons_info:
            button = tk.Button(
                main_frame,
                text=text,
                command=command,
                bg=color,
                fg="white",
                **button_style
            )
            button.pack(pady=10)
            self.buttons.append(button)
        
        self.highlight_button(0)
        
        # Status label
        self.status_label = tk.Label(
            main_frame,
            text="Ready",
            font=("Helvetica", 12),
            bg="#f0f0f0",
            fg="#333333"
        )
        self.status_label.pack(pady=10)

    def send_alert(self):
        """Run the alert email script"""
        try:
            # Import and run the m.py script
            import subprocess
            subprocess.Popen(["python", "m.py"])
            self.status_label.config(text="Alert emails being sent!")
        except Exception as e:
            self.status_label.config(text=f"Error: {str(e)}")

    def highlight_button(self, index):
        original_colors = ["#4285F4", "#2196F3", "#4CAF50", "#007ACC", "#FF9800", "#FF5252"]  # Updated with alert button color
        for i, button in enumerate(self.buttons):
            button.config(bg=original_colors[i])
        self.buttons[index].config(bg="#1a237e")

    def update_cursor_position(self):
        """Update the position of the cursor to highlight the current button"""
        if hasattr(self, 'cursor'):
            self.cursor.place_forget()
        
        if self.current_button_index < len(self.buttons):
            btn = self.buttons[self.current_button_index]
            x = btn.winfo_x() + btn.winfo_width()//2 - 5
            y = btn.winfo_y() + btn.winfo_height() + 5
            self.cursor.place(in_=btn.master, x=x, y=y)

    def move_to_next_button(self):
        self.current_button_index = (self.current_button_index + 1) % len(self.buttons)
        self.highlight_button(self.current_button_index)
        self.status_label.config(text="Moved to next button")
        self.update_cursor_position()

    def select_current_button(self):
        self.buttons[self.current_button_index].invoke()
        self.status_label.config(text="Button selected!")

    

    def launch_google_search(self):
        self.withdraw()  # Hide the launcher instead of destroying it
        vkb = VirtualKeyboard(self.visualizer)
        vkb.protocol("WM_DELETE_WINDOW", vkb.on_closing)
        vkb.mainloop()

    def launch_notepad(self):
        self.withdraw()  # Hide the launcher
        notepad = NotepadKeyboard(self.visualizer)
        notepad.protocol("WM_DELETE_WINDOW", notepad.on_closing)
        notepad.mainloop()

    def launch_vscode(self):
        self.withdraw()  # Hide the launcher
        vscode = VSCodeKeyboard(self.visualizer)
        vscode.protocol("WM_DELETE_WINDOW", vscode.on_closing)
        vscode.mainloop()

    def launch_mail(self):
        self.withdraw()  # Hide the launcher
        mail = MailKeyboard(self.visualizer)
        mail.protocol("WM_DELETE_WINDOW", mail.on_closing)
        mail.mainloop()

    def exit_program(self):
        # Stop any pending after() calls
        self.after_cancel_all()
        
        # First close any open windows
        if self.visualizer.vkb_gui:
            self.visualizer.vkb_gui.destroy()
        if self.visualizer.notepad_gui:
            self.visualizer.notepad_gui.destroy()
        if self.visualizer.vscode_gui:
            self.visualizer.vscode_gui.destroy()
        if self.visualizer.mail_gui:
            self.visualizer.mail_gui.destroy()
        
        # Stop the serial thread
        if hasattr(self.visualizer, 'serial_thread'):
            self.visualizer.serial_thread.stop()
        
        # Close the visualizer window
        self.visualizer.close()
        
        # Finally destroy this window
        self.destroy()
        QApplication.instance().quit()

    def after_cancel_all(self):
        """Cancel all pending after() calls"""
        for id in self.tk.call('after', 'info'):
            self.after_cancel(id)

class ConfigDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setWindowTitle('Configuration')
        icon_path = "Resources/Icon.png"
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        
        self.setFixedSize(370, 300)
        layout = QVBoxLayout(self)
        
        # COM Port Selection
        com_layout = QHBoxLayout()
        self.com_label = QLabel('Select COM Port:', self)
        com_layout.addWidget(self.com_label)
        self.com_select = QComboBox(self)
        self.com_select.setFixedSize(100, 25)
        self.populate_com_ports()
        com_layout.addWidget(self.com_select)
        layout.addLayout(com_layout)
        
        # Amplitude Setting
        amp_layout = QHBoxLayout()
        self.amp_label = QLabel('Set Amplitude Range:', self)
        amp_layout.addWidget(self.amp_label)
        self.amp_select = QSpinBox(self)
        self.amp_select.setFixedSize(100, 25)
        self.amp_select.setRange(-1000, 1000)
        self.amp_select.setValue(1000)
        amp_layout.addWidget(self.amp_select)
        layout.addLayout(amp_layout)
        
        # Number of Channels Setting
        ch_layout = QHBoxLayout()
        self.ch_label = QLabel('Number of Channels:', self)
        ch_layout.addWidget(self.ch_label)
        self.ch_select = QSpinBox(self)
        self.ch_select.setFixedSize(100, 25)
        self.ch_select.setRange(1, 10)
        self.ch_select.setValue(1)
        ch_layout.addWidget(self.ch_select)
        layout.addLayout(ch_layout)
        
        # Number of Seconds of Data Setting
        sec_layout = QHBoxLayout()
        self.sec_label = QLabel('Data View Duration (Seconds):', self)
        sec_layout.addWidget(self.sec_label)
        self.sec_select = QSpinBox(self)
        self.sec_select.setFixedSize(100, 25)
        self.sec_select.setRange(5, 20)
        self.sec_select.setValue(5)
        sec_layout.addWidget(self.sec_select)
        layout.addLayout(sec_layout)
        
        # OK Button
        button_layout = QHBoxLayout()
        self.ok_button = QPushButton('OK', self)
        self.ok_button.setFixedSize(100, 30)
        self.ok_button.clicked.connect(self.accept)
        button_layout.addWidget(self.ok_button)
        layout.addLayout(button_layout)

    def populate_com_ports(self):
        com_ports = serial.tools.list_ports.comports()
        available_ports = [port.device for port in com_ports]
        self.com_select.addItems(available_ports)

    def get_config(self):
        return self.com_select.currentText(), self.amp_select.value(), self.ch_select.value(), self.sec_select.value()

def create_channel_list(num_channels):
    return [f"Channel {i+1}" for i in range(num_channels)]
if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    # Show configuration dialog
    config_dialog = ConfigDialog()
    if config_dialog.exec_() == QDialog.Accepted:
        com_port, amplitude, no_of_channels, duration = config_dialog.get_config()
        channels = create_channel_list(no_of_channels)
        
        # Create SignalVisualizer
        visualizer = SignalVisualizer(
            channels=no_of_channels,
            data_length=duration*250,
            port=com_port,
            amplitude=amplitude
        )
        visualizer.show()
        
        # Create and start launcher GUI
        launcher = LauncherGUI(visualizer)
        launcher.mainloop()
        
        # Start Qt event loop
        sys.exit(app.exec_())
