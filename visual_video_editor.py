import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import os
import time
import re
import tempfile

# --- Dependency Checks ---
try:
    import customtkinter as ctk
except ImportError:
    messagebox.showerror("Missing Dependency", "Please install customtkinter:\npip install customtkinter")
    exit()

try:
    # Try MoviePy v2+ imports first
    from moviepy import VideoFileClip, AudioFileClip, ImageClip
except ImportError:
    try:
        # Fallback to MoviePy v1.x imports
        from moviepy.editor import VideoFileClip, AudioFileClip, ImageClip
    except ImportError:
        messagebox.showerror("Missing Dependency", "Please install moviepy: pip install moviepy")
        exit()

try:
    from PIL import Image, ImageTk, ImageGrab
except ImportError:
    messagebox.showerror("Missing Dependency", "Please install Pillow: pip install Pillow")
    exit()

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False
    print("Notice: tkinterdnd2 not installed. Drag and drop will be disabled.")

try:
    import ffmpeg
    import imageio_ffmpeg
except ImportError:
    messagebox.showerror("Missing Dependency", "Please install ffmpeg-python:\npip install ffmpeg-python imageio-ffmpeg")
    exit()

try:
    import sounddevice as sd
    import numpy as np
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False
    print("Notice: sounddevice or numpy not installed. Preview audio will be disabled.")

# Configure CustomTkinter Theme
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# --- CustomTkinter Drag & Drop Wrapper ---
if HAS_DND:
    class CTk_DnD(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)

class TimelineClip:
    """Represents a segment of media on the timeline."""
    def __init__(self, filepath, media_clip, timeline_pos, is_audio_only=False, is_image=False):
        self.filepath = filepath
        self.media = media_clip
        self.trim_start = 0.0
        self.is_audio_only = is_audio_only
        self.is_image = is_image
        
        if self.is_image:
            self.trim_end = getattr(media_clip, 'duration', None) or 5.0
        else:
            self.trim_end = media_clip.duration
            
        self.timeline_pos = timeline_pos
        self.track = 0  # Default to top track
        self.selected = False
        self.is_muted = False
        self.volume = 1.0
        self.fade_in = False
        self.fade_out = False

    @property
    def duration(self):
        return self.trim_end - self.trim_start
        
    def clone(self):
        """Creates a deep copy of the clip state for the Undo history and Copy/Paste."""
        c = TimelineClip(self.filepath, self.media, self.timeline_pos, self.is_audio_only, self.is_image)
        c.trim_start = self.trim_start
        c.trim_end = self.trim_end
        c.track = self.track
        c.selected = self.selected
        c.is_muted = self.is_muted
        c.volume = self.volume
        c.fade_in = self.fade_in
        c.fade_out = self.fade_out
        return c

class VisualVideoEditor:
    def __init__(self, root):
        self.root = root
        self.root.title("Visual Clip Editor")
        self.root.geometry("1050x850")
        self.root.minsize(950, 750)
        
        # --- Drag and Drop Setup ---
        if HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind('<<Drop>>', self.handle_drop)
        
        # --- State Variables ---
        self.media_cache = {}  # Caches source video/audio files to save RAM
        self.clips = []
        self.undo_stack = []
        self.redo_stack = []
        self.internal_clipboard = [] # Holds copied clips
        self.temp_files = [] # Tracks temporarily generated files for cleanup
        
        self.pixels_per_sec = 20.0  # Zoom level
        self.playhead_time = 0.0
        self.audio_playhead = 0.0
        
        # UI Track variables
        self.num_tracks = 4
        self.track_height = 50
        self.track_spacing = 5
        
        # Interaction state
        self.drag_mode = None  # 'move', 'trim_left', 'trim_right'
        self.drag_clip = None
        self.drag_offset = 0.0
        self.last_preview_time = -1
        
        # Playback state
        self.is_playing = False
        self.last_play_time = 0.0
        
        # --- Background Preview Thread ---
        self.preview_event = threading.Event()
        self.preview_request = None
        self.preview_thread = threading.Thread(target=self._preview_worker, daemon=True)
        self.preview_thread.start()
        
        # --- Background Audio Thread ---
        self.audio_thread = threading.Thread(target=self._audio_worker, daemon=True)
        self.audio_thread.start()
        
        # Hardware Acceleration Mapping
        self.codec_mapping = {
            "CPU Default (Standard)": "libx264",
            "NVIDIA GPU (NVENC)": "h264_nvenc",
            "AMD GPU (AMF)": "h264_amf",
            "Mac GPU (VideoToolbox)": "h264_videotoolbox",
            "Intel GPU (QSV)": "h264_qsv"
        }
        self.selected_codec_name = ctk.StringVar(value="CPU Default (Standard)")

        # Hook into the window closing event
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.create_widgets()
        self.setup_keybinds()
        self.draw_timeline()

    def on_closing(self):
        """Cleans up temporary files and releases media locks before exiting."""
        # 1. Delete any temporary images created via clipboard paste
        for tmp_file in getattr(self, 'temp_files', []):
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception as e:
                print(f"Warning: Could not delete temp file {tmp_file}: {e}")
        
        # 2. Safely close all cached MoviePy media files to release OS file locks
        for media in getattr(self, 'media_cache', {}).values():
            try:
                media.close()
            except Exception:
                pass
                
        # 3. Destroy the window
        self.root.destroy()

    def get_or_load_media(self, filepath, is_audio, is_image):
        """Fetches from cache or heavily loads a source file exactly once."""
        if filepath not in self.media_cache:
            if is_audio:
                self.media_cache[filepath] = AudioFileClip(filepath)
            elif is_image:
                self.media_cache[filepath] = ImageClip(filepath)
            else:
                self.media_cache[filepath] = VideoFileClip(filepath)
        return self.media_cache[filepath]

    def create_widgets(self):
        # --- Toolbar ---
        toolbar = ctk.CTkFrame(self.root, corner_radius=0, height=50)
        toolbar.pack(side="top", fill="x")
        
        ctk.CTkButton(toolbar, text="Add Media", command=self.add_media, width=120).pack(side="left", padx=10, pady=10)
        ctk.CTkButton(toolbar, text="Delete Selected", command=self.delete_selected, width=120, fg_color="#5c5c5c", hover_color="#757575").pack(side="left", padx=5, pady=10)
        
        # Undo / Redo Buttons
        ctk.CTkButton(toolbar, text="Undo", command=self.undo, width=60, fg_color="#424242", hover_color="#616161").pack(side="left", padx=(15, 2), pady=10)
        ctk.CTkButton(toolbar, text="Redo", command=self.redo, width=60, fg_color="#424242", hover_color="#616161").pack(side="left", padx=2, pady=10)
        
        ctk.CTkButton(toolbar, text="Export Video", command=self.open_export_settings, width=140, fg_color="#2E7D32", hover_color="#388E3C").pack(side="right", padx=10, pady=10)
        
        # --- Timeline Area ---
        timeline_container = ctk.CTkFrame(self.root, height=310, corner_radius=0)
        timeline_container.pack(side="bottom", fill="x")
        timeline_container.pack_propagate(False)
        
        zoom_frame = ctk.CTkFrame(timeline_container, corner_radius=0, fg_color="transparent")
        zoom_frame.pack(side="top", fill="x", padx=5, pady=5)
        
        ctk.CTkLabel(zoom_frame, text="Timeline Tools:").pack(side="left", padx=5)
        ctk.CTkButton(zoom_frame, text="Zoom In (+)", command=self.zoom_in, width=100, fg_color="#424242", hover_color="#616161").pack(side="right", padx=5)
        ctk.CTkButton(zoom_frame, text="Zoom Out (-)", command=self.zoom_out, width=100, fg_color="#424242", hover_color="#616161").pack(side="right", padx=5)

        # --- Playback Controls ---
        playback_frame = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        playback_frame.pack(side="bottom", fill="x", pady=10)
        
        self.mute_preview_var = ctk.BooleanVar(value=False)
        self.mute_checkbox = ctk.CTkCheckBox(playback_frame, text="Mute Preview", variable=self.mute_preview_var)
        self.mute_checkbox.pack(side="right", padx=10)
        
        self.time_display_label = ctk.CTkLabel(playback_frame, text="00:00:00.000  |  Frame: 0", font=("Consolas", 13))
        self.time_display_label.pack(side="right", padx=20)
        
        btn_container = ctk.CTkFrame(playback_frame, fg_color="transparent")
        btn_container.pack(expand=True)
        
        ctk.CTkButton(btn_container, text="⏪ 5s", command=self.skip_back, width=60, fg_color="#424242", hover_color="#616161").pack(side="left", padx=5)
        ctk.CTkButton(btn_container, text="< 1f", command=self.step_frame_back, width=50, fg_color="#424242", hover_color="#616161").pack(side="left", padx=2)
        
        self.play_button = ctk.CTkButton(btn_container, text="Play", command=self.toggle_play, width=100, fg_color="#2E7D32", hover_color="#388E3C")
        self.play_button.pack(side="left", padx=10)
        
        ctk.CTkButton(btn_container, text="1f >", command=self.step_frame_forward, width=50, fg_color="#424242", hover_color="#616161").pack(side="left", padx=2)
        ctk.CTkButton(btn_container, text="5s ⏩", command=self.skip_forward, width=60, fg_color="#424242", hover_color="#616161").pack(side="left", padx=5)

        # --- Dual Canvas Setup ---
        canvas_frame = ctk.CTkFrame(timeline_container, corner_radius=0, fg_color="transparent")
        canvas_frame.pack(side="top", fill="both", expand=True)

        self.v_scroll = ctk.CTkScrollbar(canvas_frame, orientation="vertical")
        self.v_scroll.pack(side="right", fill="y", padx=(2,0))

        # Using Tkinter Canvas here because CTk doesnt have a drawing canvas
        self.time_canvas = tk.Canvas(canvas_frame, bg="#202124", height=25, highlightthickness=0)
        self.time_canvas.pack(side="top", fill="x")

        self.track_canvas = tk.Canvas(canvas_frame, bg="#2b2d30", highlightthickness=0, yscrollcommand=self.v_scroll.set)
        self.track_canvas.pack(side="top", fill="both", expand=True)

        self.v_scroll.configure(command=self.track_canvas.yview)

        self.h_scroll = ctk.CTkScrollbar(timeline_container, orientation="horizontal")
        self.h_scroll.pack(side="bottom", fill="x", pady=(2,0))
        
        def sync_h_scroll(*args):
            self.time_canvas.xview(*args)
            self.track_canvas.xview(*args)

        self.h_scroll.configure(command=sync_h_scroll)
        self.time_canvas.config(xscrollcommand=self.h_scroll.set)
        self.track_canvas.config(xscrollcommand=self.h_scroll.set)

        # --- Preview Area ---
        self.preview_frame = ctk.CTkFrame(self.root, corner_radius=0, fg_color="black")
        self.preview_frame.pack(side="top", fill="both", expand=True)
        self.preview_frame.pack_propagate(False)
        
        self.preview_label = ctk.CTkLabel(self.preview_frame, text="No Video Loaded\n(Drag & Drop Video Here)", fg_color="black")
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        
        # Bindings for Time Canvas
        self.time_canvas.bind("<Button-1>", self.on_left_click)
        self.time_canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.time_canvas.bind("<ButtonRelease-1>", self.on_mouse_release)

        # Bindings for Track Canvas
        self.track_canvas.bind("<Button-1>", self.on_left_click)
        self.track_canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.track_canvas.bind("<ButtonRelease-1>", self.on_mouse_release)
        self.track_canvas.bind("<Button-3>", self.on_right_click)
        
        for c in (self.time_canvas, self.track_canvas):
            c.bind("<MouseWheel>", self.on_mousewheel)
            c.bind("<Button-4>", self.on_mousewheel)
            c.bind("<Button-5>", self.on_mousewheel)
        
        self.root.bind("<Configure>", lambda e: self.update_preview_image())

    def get_fps(self):
        """Helper to get a valid framerate depending on what clips exist."""
        for c in self.clips:
            if not c.is_audio_only and not getattr(c, 'is_image', False) and hasattr(c.media, 'fps') and c.media.fps:
                return c.media.fps
        return 30.0

    def get_video_size(self):
        """Helper to get a master video size."""
        for c in self.clips:
            if not c.is_audio_only and hasattr(c.media, 'size') and c.media.size:
                return c.media.size
        return (1920, 1080)

    def setup_keybinds(self):
        self.root.bind("<Delete>", lambda e: self.delete_selected())
        self.root.bind("<BackSpace>", lambda e: self.delete_selected())
        self.root.bind("<space>", self.on_space_pressed)
        self.root.bind("<Left>", lambda e: self.skip_back())
        self.root.bind("<Right>", lambda e: self.skip_forward())
        self.root.bind("<comma>", lambda e: self.step_frame_back())
        self.root.bind("<period>", lambda e: self.step_frame_forward())
        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-y>", lambda e: self.redo())
        self.root.bind("<Control-Z>", lambda e: self.redo()) # Shift+Z support
        self.root.bind("<Control-c>", lambda e: self.copy_selected())
        self.root.bind("<Control-x>", lambda e: self.cut_selected())
        self.root.bind("<Control-v>", lambda e: self.paste())
        self.root.bind("<Control-C>", lambda e: self.copy_selected())
        self.root.bind("<Control-X>", lambda e: self.cut_selected())
        self.root.bind("<Control-V>", lambda e: self.paste())

    def save_state(self):
        state = [c.clone() for c in self.clips]
        self.undo_stack.append(state)
        if len(self.undo_stack) > 50: # Limit history
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        if self.undo_stack:
            current_state = [c.clone() for c in self.clips]
            self.redo_stack.append(current_state)
            self.clips = self.undo_stack.pop()
            self.draw_timeline()
            self.update_preview_image()

    def redo(self):
        if self.redo_stack:
            current_state = [c.clone() for c in self.clips]
            self.undo_stack.append(current_state)
            self.clips = self.redo_stack.pop()
            self.draw_timeline()
            self.update_preview_image()

    def on_space_pressed(self, event):
        self.toggle_play()
        return "break"

    def handle_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        valid = [fp for fp in files if any(fp.lower().endswith(ext) for ext in ['.mp4', '.avi', '.mov', '.mkv', '.mp3', '.wav', '.m4a', '.ogg', '.aac', '.png', '.jpg', '.jpeg', '.bmp', '.webp'])]
        if valid:
            self.save_state()
            for filepath in valid:
                self.add_media_from_path(filepath, auto_save=False)
            self.draw_timeline()
            self.update_preview_image()

    def zoom_in(self):
        self.pixels_per_sec = min(300.0, self.pixels_per_sec * 1.3)
        self.draw_timeline()

    def zoom_out(self):
        self.pixels_per_sec = max(2.0, self.pixels_per_sec / 1.3)
        self.draw_timeline()

    def on_mousewheel(self, event):
        if event.state & 0x0004:
            if event.num == 4 or event.delta > 0:
                self.zoom_in()
            elif event.num == 5 or event.delta < 0:
                self.zoom_out()

    # --- Clipboard / Copy / Paste Logic ---
    def copy_selected(self, event=None):
        selected = [c for c in self.clips if c.selected]
        if selected:
            # Sort chronologically so they paste in the correct original order
            selected.sort(key=lambda c: c.timeline_pos)
            self.internal_clipboard = [c.clone() for c in selected]
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append("VisualVideoEditor: Internal Clip(s)")
            except:
                pass

    def cut_selected(self, event=None):
        self.copy_selected()
        self.delete_selected()

    def _get_clipboard_files_windows(self):
        """Magically grabs file paths natively copied from Windows Explorer."""
        files = []
        if os.name == 'nt':
            try:
                import ctypes
                ctypes.windll.user32.OpenClipboard(0)
                if ctypes.windll.user32.IsClipboardFormatAvailable(15): # CF_HDROP
                    hdrop = ctypes.windll.user32.GetClipboardData(15)
                    count = ctypes.windll.shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                    for i in range(count):
                        buffer = ctypes.create_unicode_buffer(260)
                        ctypes.windll.shell32.DragQueryFileW(hdrop, i, buffer, 260)
                        files.append(buffer.value)
                ctypes.windll.user32.CloseClipboard()
            except Exception:
                pass
        return files

    def paste(self, event=None):
        # 1. Attempt to grab actual files copied from Windows File Explorer
        valid_paths = self._get_clipboard_files_windows()
        
        # 2. Check for raw image data in clipboard (e.g. copied from website snippet)
        if not valid_paths:
            try:
                img = ImageGrab.grabclipboard()
                if isinstance(img, Image.Image):
                    # Save the image to a temporary file
                    temp_dir = tempfile.gettempdir()
                    temp_path = os.path.join(temp_dir, f"pasted_image_{int(time.time()*1000)}.png")
                    img.save(temp_path)
                    valid_paths.append(temp_path)
                    self.temp_files.append(temp_path) # Track it so we can delete it later
            except Exception:
                pass
        
        # 3. If no files or images, check if the system clipboard holds text that happens to be valid file paths
        if not valid_paths:
            try:
                sys_clip = self.root.clipboard_get()
                potential_paths = [p.strip(' "\'\t\r\n') for p in sys_clip.splitlines()]
                valid_paths = [p for p in potential_paths if os.path.exists(p) and any(p.lower().endswith(ext) for ext in ['.mp4', '.avi', '.mov', '.mkv', '.mp3', '.wav', '.m4a', '.ogg', '.aac', '.png', '.jpg', '.jpeg', '.bmp', '.webp'])]
            except Exception:
                pass

        # External File/Image Paste
        if valid_paths:
            self.save_state()
            current_paste_time = self.playhead_time
            for p in valid_paths:
                clip = self.add_media_from_path(p, start_pos=current_paste_time, auto_save=False)
                if clip:
                    current_paste_time += clip.duration
            self.draw_timeline()
            self.update_preview_image()
            return
            
        # Internal App Clip Paste
        if hasattr(self, 'internal_clipboard') and self.internal_clipboard:
            self.save_state()
            base_time = self.internal_clipboard[0].timeline_pos
            offset = self.playhead_time - base_time
            
            for c in self.clips:
                c.selected = False
                
            for copied_clip in self.internal_clipboard:
                new_clip = copied_clip.clone()
                new_clip.timeline_pos += offset
                new_clip.selected = True
                
                # Push clip down to nearest empty track to avoid collisions
                track = new_clip.track
                while self.would_overlap(new_clip.timeline_pos, new_clip.duration, track):
                    track += 1
                    if track >= self.num_tracks:
                        track = new_clip.track # Out of tracks, let it overlap where it started
                        break
                new_clip.track = track
                
                self.clips.append(new_clip)
                
            self.draw_timeline()
            self.update_preview_image()

    # --- Playback Logic ---
    def toggle_play(self):
        if not self.clips:
            return
            
        self.is_playing = not self.is_playing
        if self.is_playing:
            self.play_button.configure(text="Pause", fg_color="#C62828", hover_color="#D32F2F")
            self.last_play_time = time.time()
            self.audio_playhead = self.playhead_time
            self.play_loop()
        else:
            self.play_button.configure(text="Play", fg_color="#2E7D32", hover_color="#388E3C")

    def play_loop(self):
        if not self.is_playing:
            return

        current_time = time.time()
        delta = current_time - self.last_play_time
        self.last_play_time = current_time

        # Let the real-time audio thread drive the playback timing to prevent drift
        if HAS_SOUNDDEVICE and not self.mute_preview_var.get():
            self.playhead_time = self.audio_playhead
        else:
            self.playhead_time += delta
            self.audio_playhead = self.playhead_time

        furthest_edge = max((c.timeline_pos + c.duration for c in self.clips)) if self.clips else 0
        
        # --- Auto-Scroll Logic ---
        max_canvas_time = max(60.0, furthest_edge + 30.0)
        x_frac_start, x_frac_end = self.time_canvas.xview()
        visible_start_time = x_frac_start * max_canvas_time
        visible_end_time = x_frac_end * max_canvas_time
        visible_width_time = visible_end_time - visible_start_time
        
        # Scroll if playhead enters the last 10% of the visible view
        if self.playhead_time > visible_end_time - (visible_width_time * 0.1):
            new_start_frac = min(1.0 - (visible_width_time / max_canvas_time), x_frac_start + (visible_width_time * 0.5) / max_canvas_time)
            if max_canvas_time > 0 and new_start_frac > x_frac_start:
                self.time_canvas.xview_moveto(new_start_frac)
                self.track_canvas.xview_moveto(new_start_frac)
        # -----------------------

        if self.playhead_time >= furthest_edge and furthest_edge > 0:
            self.playhead_time = furthest_edge
            self.audio_playhead = furthest_edge
            self.draw_timeline()
            self.update_preview_image()
            self.toggle_play()
            return

        self.draw_timeline()
        self.update_preview_image(fast_mode=True)

        if self.is_playing:
            self.root.after(33, self.play_loop)

    def skip_back(self):
        self.playhead_time = max(0.0, self.playhead_time - 5.0)
        self.audio_playhead = self.playhead_time
        self.draw_timeline()
        self.update_preview_image()

    def skip_forward(self):
        self.playhead_time += 5.0
        max_time = max((c.timeline_pos + c.duration for c in self.clips)) if self.clips else 0
        if max_time > 0:
            self.playhead_time = min(self.playhead_time, max_time)
        self.audio_playhead = self.playhead_time
        self.draw_timeline()
        self.update_preview_image()

    def step_frame_back(self):
        fps = self.get_fps()
        self.playhead_time = max(0.0, self.playhead_time - 1.0/fps)
        self.audio_playhead = self.playhead_time
        self.draw_timeline()
        self.update_preview_image(fast_mode=False)

    def step_frame_forward(self):
        fps = self.get_fps()
        max_time = max((c.timeline_pos + c.duration for c in self.clips)) if self.clips else 0.0
        if max_time > 0:
            self.playhead_time = min(max_time, self.playhead_time + 1.0/fps)
        else:
            self.playhead_time += 1.0/fps
        self.audio_playhead = self.playhead_time
        self.draw_timeline()
        self.update_preview_image(fast_mode=False)

    def cut_at_playhead(self):
        clips_to_split = [c for c in self.clips if c.timeline_pos < self.playhead_time < (c.timeline_pos + c.duration - 0.5)]
        if clips_to_split:
            self.save_state()
            for c in clips_to_split:
                self.split_clip(c, self.playhead_time, skip_save=True)

    # --- Interaction Logic ---
    def get_time_from_x(self, x, canvas):
        canvas_x = canvas.canvasx(x)
        return max(0.0, canvas_x / self.pixels_per_sec)

    def get_track_from_y(self, y):
        t = int(y / (self.track_height + self.track_spacing))
        return max(0, min(self.num_tracks - 1, t))

    def get_clip_at_mouse(self, x, y, canvas):
        time_sec = self.get_time_from_x(x, canvas)
        track_idx = self.get_track_from_y(y)
        for clip in reversed(self.clips):
            if clip.track == track_idx and clip.timeline_pos <= time_sec <= (clip.timeline_pos + clip.duration):
                return clip
        return None

    def would_overlap(self, time_start, duration, track_idx, ignore_clip=None):
        time_end = time_start + duration
        for c in self.clips:
            if c != ignore_clip and c.track == track_idx:
                if time_start < c.timeline_pos + c.duration and time_end > c.timeline_pos:
                    return True
        return False

    def on_left_click(self, event):
        if self.is_playing:
            self.toggle_play()

        click_time = self.get_time_from_x(event.x, event.widget)
        
        # Save state before a drag potentially starts
        self.pre_drag_state = [c.clone() for c in self.clips]
        self.has_dragged = False
        
        if event.widget == self.time_canvas:
            self.playhead_time = click_time
            self.audio_playhead = click_time
            self.drag_mode = 'scrub'
            self.draw_timeline()
            self.update_preview_image()
            return
        
        for c in self.clips:
            c.selected = False
            
        clicked_clip = self.get_clip_at_mouse(event.x, event.y, event.widget)
        self.drag_clip = clicked_clip
        
        if clicked_clip:
            clicked_clip.selected = True
            edge_threshold = 5.0 / self.pixels_per_sec
            
            start_time = clicked_clip.timeline_pos
            end_time = clicked_clip.timeline_pos + clicked_clip.duration
            
            if abs(click_time - start_time) < edge_threshold:
                self.drag_mode = 'trim_left'
            elif abs(click_time - end_time) < edge_threshold:
                self.drag_mode = 'trim_right'
            else:
                self.drag_mode = 'move'
                self.drag_offset = click_time - clicked_clip.timeline_pos
        else:
            self.drag_mode = 'pan'
            self.track_canvas.scan_mark(event.x, event.y)

        self.draw_timeline()
        self.update_preview_image()

    def on_mouse_drag(self, event):
        current_time = self.get_time_from_x(event.x, event.widget)
        
        if self.drag_mode == 'scrub':
            self.playhead_time = current_time
            self.audio_playhead = current_time
            self.draw_timeline()
            if time.time() - self.last_preview_time > 0.05:
                self.update_preview_image(fast_mode=True)
                self.last_preview_time = time.time()
            return
            
        if self.drag_mode == 'pan':
            self.track_canvas.scan_dragto(event.x, event.y, gain=1)
            x_frac = self.track_canvas.xview()[0]
            if x_frac <= 0.0:
                self.track_canvas.xview_moveto(0.0)
                x_frac = 0.0
            self.time_canvas.xview_moveto(x_frac)
            self.track_canvas.scan_mark(event.x, event.y)
            return

        if self.drag_clip and self.drag_mode in ['move', 'trim_left', 'trim_right']:
            self.has_dragged = True
            clip = self.drag_clip
            ctrl_held = (event.state & 0x0004) or (event.state & 0x0008)
            snap_threshold = 15.0 / self.pixels_per_sec
            
            if self.drag_mode == 'move':
                new_track = self.get_track_from_y(event.y)
                new_time = max(0, current_time - self.drag_offset)
                
                # Snap Logic
                if not ctrl_held:
                    snap_points = [self.playhead_time]
                    for c in self.clips:
                        if c != clip:
                            snap_points.extend([c.timeline_pos, c.timeline_pos + c.duration])
                    
                    best_snap = None
                    min_dist = float('inf')
                    for p in snap_points:
                        if abs(p - new_time) < min_dist and abs(p - new_time) < snap_threshold:
                            min_dist = abs(p - new_time)
                            best_snap = p
                        if abs(p - (new_time + clip.duration)) < min_dist and abs(p - (new_time + clip.duration)) < snap_threshold:
                            min_dist = abs(p - (new_time + clip.duration))
                            best_snap = p - clip.duration
                            
                    if best_snap is not None:
                        new_time = best_snap

                # Collision Logic
                if not self.would_overlap(new_time, clip.duration, new_track, ignore_clip=clip):
                    clip.timeline_pos = new_time
                    clip.track = new_track
                else:
                    best_fallback = None
                    min_dist = float('inf')
                    for c in self.clips:
                        if c != clip and c.track == new_track:
                            if new_time < c.timeline_pos + c.duration and new_time + clip.duration > c.timeline_pos:
                                t1 = c.timeline_pos - clip.duration
                                if t1 >= 0 and not self.would_overlap(t1, clip.duration, new_track, ignore_clip=clip):
                                    if abs(t1 - new_time) < min_dist:
                                        min_dist = abs(t1 - new_time)
                                        best_fallback = t1
                                
                                t2 = c.timeline_pos + c.duration
                                if not self.would_overlap(t2, clip.duration, new_track, ignore_clip=clip):
                                    if abs(t2 - new_time) < min_dist:
                                        min_dist = abs(t2 - new_time)
                                        best_fallback = t2
                    if best_fallback is not None:
                        clip.timeline_pos = best_fallback
                        clip.track = new_track

            elif self.drag_mode == 'trim_left':
                delta = current_time - clip.timeline_pos
                
                if not ctrl_held:
                    snap_points = [self.playhead_time]
                    for c in self.clips:
                        if c != clip:
                            snap_points.extend([c.timeline_pos, c.timeline_pos + c.duration])
                    for p in snap_points:
                        if abs(p - current_time) < snap_threshold:
                            delta = p - clip.timeline_pos
                            break
                            
                new_trim_start = clip.trim_start + delta
                new_pos = clip.timeline_pos + delta
                
                min_pos = 0.0
                for c in self.clips:
                    if c != clip and c.track == clip.track and c.timeline_pos <= clip.timeline_pos:
                        min_pos = max(min_pos, c.timeline_pos + c.duration)
                        
                if new_pos < min_pos:
                    delta = min_pos - clip.timeline_pos
                    new_trim_start = clip.trim_start + delta
                    new_pos = min_pos
                    
                if 0 <= new_trim_start < clip.trim_end - 0.5:
                    clip.trim_start = new_trim_start
                    clip.timeline_pos = new_pos

            elif self.drag_mode == 'trim_right':
                delta = current_time - (clip.timeline_pos + clip.duration)
                
                if not ctrl_held:
                    snap_points = [self.playhead_time]
                    for c in self.clips:
                        if c != clip:
                            snap_points.extend([c.timeline_pos, c.timeline_pos + c.duration])
                    for p in snap_points:
                        if abs(p - current_time) < snap_threshold:
                            delta = p - (clip.timeline_pos + clip.duration)
                            break

                new_trim_end = clip.trim_end + delta
                new_duration = new_trim_end - clip.trim_start
                
                max_pos = float('inf')
                for c in self.clips:
                    if c != clip and c.track == clip.track and c.timeline_pos >= clip.timeline_pos:
                        max_pos = min(max_pos, c.timeline_pos)
                        
                if clip.timeline_pos + new_duration > max_pos:
                    new_duration = max_pos - clip.timeline_pos
                    new_trim_end = clip.trim_start + new_duration
                
                max_source_dur = clip.media.duration if not getattr(clip, 'is_image', False) else float('inf')
                if clip.trim_start + 0.5 < new_trim_end <= max_source_dur:
                    clip.trim_end = new_trim_end

            self.draw_timeline()
            if time.time() - self.last_preview_time > 0.05:
                self.update_preview_image(fast_mode=True)
                self.last_preview_time = time.time()

    def on_mouse_release(self, event):
        if getattr(self, 'has_dragged', False) and self.drag_mode in ['move', 'trim_left', 'trim_right']:
            # Push the saved pre-drag state to undo stack
            self.undo_stack.append(self.pre_drag_state)
            if len(self.undo_stack) > 50:
                self.undo_stack.pop(0)
            self.redo_stack.clear()
            
        self.drag_mode = None
        self.drag_clip = None
        self.has_dragged = False
        self.update_preview_image()

    def on_right_click(self, event):
        if event.widget == self.time_canvas:
            return
            
        if self.is_playing:
            self.toggle_play()

        click_time = self.get_time_from_x(event.x, event.widget)
        clip_to_split = self.get_clip_at_mouse(event.x, event.y, event.widget)
        
        for c in self.clips:
            c.selected = False
            
        if clip_to_split:
            clip_to_split.selected = True
        
        self.draw_timeline()
        self.update_preview_image()
        
        # Keep standard Tkinter Menu (CustomTkinter doesn't have a direct replacement)
        # Styled to fit the dark theme
        menu = tk.Menu(self.root, tearoff=0, bg="#2b2b2b", fg="white", activebackground="#1f538d", activeforeground="white", borderwidth=0)
        
        if clip_to_split:
            menu.add_command(label="Copy", command=self.copy_selected)
            menu.add_command(label="Cut", command=self.cut_selected)
            menu.add_separator()
            
            menu.add_command(label="Cut Here", command=lambda: self.split_clip(clip_to_split, click_time))
            
            if not getattr(clip_to_split, 'is_image', False):
                mute_label = "Unmute Audio" if clip_to_split.is_muted else "Mute Audio"
                menu.add_command(label=mute_label, command=lambda: self.toggle_mute(clip_to_split))
                
                vol_menu = tk.Menu(menu, tearoff=0, bg="#2b2b2b", fg="white", activebackground="#1f538d", activeforeground="white", borderwidth=0)
                for v in [0.25, 0.5, 1.0, 1.5, 2.0, 3.0]:
                    vol_menu.add_command(label=f"{int(v*100)}%", command=lambda val=v: self.set_clip_volume(clip_to_split, val))
                menu.add_cascade(label=f"Volume ({int(clip_to_split.volume*100)}%)", menu=vol_menu)
                
                menu.add_separator()
                
                menu.add_command(label=f"Fade In (1s) : {'ON' if clip_to_split.fade_in else 'OFF'}", command=lambda: self.toggle_fade(clip_to_split, 'in'))
                menu.add_command(label=f"Fade Out (1s) : {'ON' if clip_to_split.fade_out else 'OFF'}", command=lambda: self.toggle_fade(clip_to_split, 'out'))
                
                menu.add_separator()
            
        menu.add_command(label="Paste", command=self.paste)
        menu.add_separator()
        menu.add_command(label="Cut at Playhead", command=self.cut_at_playhead)
        
        if clip_to_split or any(c.selected for c in self.clips):
            menu.add_separator()
            menu.add_command(label="Delete", command=self.delete_selected)
            
        menu.post(event.x_root, event.y_root)

    def toggle_mute(self, clip):
        self.save_state()
        clip.is_muted = not clip.is_muted
        self.draw_timeline()

    def set_clip_volume(self, clip, val):
        self.save_state()
        clip.volume = val
        self.draw_timeline()
        
    def toggle_fade(self, clip, fade_type):
        self.save_state()
        if fade_type == 'in':
            clip.fade_in = not clip.fade_in
        else:
            clip.fade_out = not clip.fade_out
        self.draw_timeline()

    def split_clip(self, clip_to_split, click_time, skip_save=False):
        split_point_in_clip = click_time - clip_to_split.timeline_pos
        split_point_in_source = clip_to_split.trim_start + split_point_in_clip
        
        if split_point_in_clip < 0.5 or (clip_to_split.duration - split_point_in_clip) < 0.5:
            return

        if not skip_save:
            self.save_state()

        is_audio = clip_to_split.is_audio_only
        is_img = getattr(clip_to_split, 'is_image', False)
        media = self.get_or_load_media(clip_to_split.filepath, is_audio, is_img)

        new_clip = TimelineClip(clip_to_split.filepath, media, click_time, is_audio_only=is_audio, is_image=is_img)
        new_clip.trim_start = split_point_in_source
        new_clip.trim_end = clip_to_split.trim_end
        new_clip.is_muted = clip_to_split.is_muted
        new_clip.volume = clip_to_split.volume
        new_clip.fade_in = False
        new_clip.fade_out = clip_to_split.fade_out
        new_clip.track = clip_to_split.track  
        
        clip_to_split.trim_end = split_point_in_source
        clip_to_split.fade_out = False
        
        self.clips.append(new_clip)
        self.draw_timeline()
        self.update_preview_image()

    def delete_selected(self):
        if any(c.selected for c in self.clips):
            self.save_state()
            self.clips = [c for c in self.clips if not c.selected]
            self.draw_timeline()
            self.update_preview_image()

    # --- Video & Drawing Logic ---
    def add_media(self):
        filepaths = filedialog.askopenfilenames(filetypes=[("Media Files", "*.mp4 *.avi *.mov *.mkv *.mp3 *.wav *.m4a *.ogg *.aac *.png *.jpg *.jpeg *.bmp *.webp")])
        if filepaths:
            self.save_state()
            current_time = self.playhead_time
            for fp in filepaths:
                clip = self.add_media_from_path(fp, start_pos=current_time, auto_save=False)
                if clip:
                    current_time += clip.duration
                    
            self.draw_timeline()
            if len(self.clips) == len(filepaths):
                self.playhead_time = 0.0
                self.audio_playhead = 0.0
                self.update_preview_image()

    def add_media_from_path(self, filepath, start_pos=None, auto_save=True):
        try:
            ext = filepath.lower().split('.')[-1]
            is_audio = ext in ['mp3', 'wav', 'm4a', 'ogg', 'aac']
            is_image = ext in ['png', 'jpg', 'jpeg', 'bmp', 'webp']
            
            # Use cached media loader so memory stays low on duplicates
            media = self.get_or_load_media(filepath, is_audio, is_image)
                
            if start_pos is None:
                start_pos = 0.0
                track0_clips = [c for c in self.clips if c.track == 0]
                if track0_clips:
                    last_clip = max(track0_clips, key=lambda c: c.timeline_pos + c.duration)
                    start_pos = last_clip.timeline_pos + last_clip.duration
                
            if auto_save:
                self.save_state()
            
            new_clip = TimelineClip(filepath, media, start_pos, is_audio_only=is_audio, is_image=is_image)
            
            # Find closest safe track 
            track = 0
            while self.would_overlap(start_pos, new_clip.duration, track):
                track += 1
                if track >= self.num_tracks:
                    track = 0  # Fallback to overlap if completely full
                    break
            new_clip.track = track
            
            self.clips.append(new_clip)
            
            if auto_save:
                self.draw_timeline()
                if len(self.clips) == 1:
                    self.playhead_time = 0.0
                    self.audio_playhead = 0.0
                    self.update_preview_image()
            
            return new_clip
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load media:\n{e}")
            return None

    def draw_timeline(self):
        self.time_canvas.delete("all")
        self.track_canvas.delete("all")
        
        max_time = 60.0
        if self.clips:
            furthest_edge = max((c.timeline_pos + c.duration for c in self.clips))
            max_time = max(max_time, furthest_edge + 30.0)
            
        canvas_width = max_time * self.pixels_per_sec
        canvas_height = self.num_tracks * (self.track_height + self.track_spacing)
        
        self.time_canvas.config(scrollregion=(0, 0, canvas_width, 25))
        self.track_canvas.config(scrollregion=(0, 0, canvas_width, canvas_height))
        
        for i in range(0, int(max_time), 5):
            x = i * self.pixels_per_sec
            self.time_canvas.create_line(x, 15, x, 25, fill="#888888")
            self.time_canvas.create_text(x + 2, 8, text=f"{i}s", fill="#cccccc", anchor="w", font=("Arial", 8))
            self.track_canvas.create_line(x, 0, x, canvas_height, fill="#3a3c3f", dash=(2, 4))
            
        for t in range(self.num_tracks):
            y1 = t * (self.track_height + self.track_spacing)
            y2 = y1 + self.track_height
            self.track_canvas.create_rectangle(0, y1, canvas_width, y2, fill="#212224", outline="")
            self.track_canvas.create_text(5, y1 + 5, text=f"Track {t+1}", fill="#777777", anchor="nw", font=("Arial", 7, "bold"))
            
        for clip in self.clips:
            x1 = clip.timeline_pos * self.pixels_per_sec
            x2 = (clip.timeline_pos + clip.duration) * self.pixels_per_sec
            
            y1 = clip.track * (self.track_height + self.track_spacing)
            y2 = y1 + self.track_height
            
            # Change color based on clip type
            has_audio_icon = True
            if clip.is_audio_only:
                filename = "🎵 " + os.path.basename(clip.filepath)
                color = "#2E7D32" if not clip.selected else "#4CAF50"
                handle_color = "#1B5E20"
            elif getattr(clip, 'is_image', False):
                filename = "🖼️ " + os.path.basename(clip.filepath)
                color = "#8d6e1f" if not clip.selected else "#c7a530"
                handle_color = "#5d4814"
                has_audio_icon = False
            else:
                filename = os.path.basename(clip.filepath)
                color = "#1f538d" if not clip.selected else "#3074c7"
                handle_color = "#14375d"
                
            outline = "#ffffff" if clip.selected else ""
            
            self.track_canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline=outline, width=1)
            
            handle_width = 8
            self.track_canvas.create_rectangle(x1, y1, x1 + handle_width, y2, fill=handle_color, outline="")
            self.track_canvas.create_rectangle(x2 - handle_width, y1, x2, y2, fill=handle_color, outline="")
            
            # Visual fade indicators
            if clip.fade_in:
                fw = min(x2-x1, self.pixels_per_sec)
                self.track_canvas.create_polygon(x1, y1, x1+fw, y1, x1, y2, fill="#181818", outline="")
            if clip.fade_out:
                fw = min(x2-x1, self.pixels_per_sec)
                self.track_canvas.create_polygon(x2, y1, x2-fw, y1, x2, y2, fill="#181818", outline="")
            
            self.track_canvas.create_text(x1 + 12, y1 + 15, text=filename, fill="white", anchor="w", font=("Arial", 10))
            
            time_txt = f"{clip.trim_start:.1f}s - {clip.trim_end:.1f}s"
            self.track_canvas.create_text(x1 + 12, y1 + 35, text=time_txt, fill="#dddddd", anchor="w", font=("Arial", 8))
            
            if has_audio_icon:
                audio_icon = "🔇" if clip.is_muted else "🔊"
                icon_x_pos = max(x1 + 30, x2 - 20)
                self.track_canvas.create_text(icon_x_pos, y1 + 25, text=audio_icon, fill="white", font=("Arial", 14))

        px = self.playhead_time * self.pixels_per_sec
        self.time_canvas.create_polygon(px-5, 0, px+5, 0, px, 10, fill="#E53935")
        self.time_canvas.create_line(px, 0, px, 25, fill="#E53935", width=2)
        self.track_canvas.create_line(px, 0, px, canvas_height, fill="#E53935", width=2)

        fps = self.get_fps()
        frame = int(self.playhead_time * fps)
        h = int(self.playhead_time // 3600)
        m = int((self.playhead_time % 3600) // 60)
        s = self.playhead_time % 60
        self.time_display_label.configure(text=f"{h:02d}:{m:02d}:{s:06.3f}  |  Frame: {frame}")

    def update_preview_image(self, fast_mode=False):
        target_width = self.preview_frame.winfo_width()
        target_height = self.preview_frame.winfo_height()
        
        if target_width < 10 or target_height < 10:
            return

        self.preview_request = (self.playhead_time, (target_width, target_height), fast_mode)
        self.preview_event.set()

    def _preview_worker(self):
        while True:
            self.preview_event.wait()
            self.preview_event.clear()
            
            if not self.preview_request:
                continue
                
            playhead_time, target_size, fast_mode = self.preview_request
            
            try:
                clips_at_time = [c for c in self.clips if c.timeline_pos <= playhead_time <= c.timeline_pos + c.duration]
                is_empty = len(clips_at_time) == 0
                
                base_img = Image.new('RGB', target_size, (0, 0, 0))
                resample_filter = Image.Resampling.NEAREST if fast_mode else Image.Resampling.BILINEAR
                
                if not is_empty:
                    clips_at_time.sort(key=lambda c: c.track, reverse=True)
                    
                    for clip in clips_at_time:
                        if clip.is_audio_only:
                            continue
                            
                        if getattr(clip, 'is_image', False):
                            time_in_source = 0.0 # Images are static
                        else:
                            time_in_source = clip.trim_start + (playhead_time - clip.timeline_pos)
                            # Prevent MoviePy from reading exactly at EOF and throwing an empty byte warning
                            time_in_source = max(0, min(time_in_source, clip.media.duration - 0.05))
                        
                        frame = clip.media.get_frame(time_in_source)
                        img = Image.fromarray(frame)
                        
                        # Fix: Maintain aspect ratio while fitting into target_size
                        img.thumbnail(target_size, resample_filter)
                        
                        # Fix: Calculate the offset to perfectly center the image on the black background
                        x_offset = (target_size[0] - img.width) // 2
                        y_offset = (target_size[1] - img.height) // 2
                        
                        base_img.paste(img, (x_offset, y_offset))
                        
                # Provide the generated image (even if blank) to entirely avoid CTkLabel image=None bug
                self.root.after(0, self._apply_preview_image, base_img, is_empty)
            except Exception:
                pass

    def _audio_worker(self):
        if not HAS_SOUNDDEVICE:
            return

        samplerate = 44100
        channels = 2
        chunk_frames = int(samplerate * 0.05) # Render in 50ms intervals

        try:
            stream = sd.OutputStream(samplerate=samplerate, channels=channels, dtype='float32')
            stream.start()
        except Exception as e:
            print("Failed to start audio stream:", e)
            return

        while True:
            if self.is_playing and not self.mute_preview_var.get():
                start_time = self.audio_playhead
                end_time = start_time + (chunk_frames / samplerate)

                audio_data = np.zeros((chunk_frames, channels), dtype=np.float32)

                clips_to_mix = [c for c in self.clips if c.timeline_pos < end_time and (c.timeline_pos + c.duration) > start_time and not c.is_muted]

                for c in clips_to_mix:
                    if getattr(c, 'is_image', False):
                        continue

                    clip_start_play = max(start_time, c.timeline_pos)
                    clip_end_play = min(end_time, c.timeline_pos + c.duration)

                    start_idx = int(round((clip_start_play - start_time) * samplerate))
                    end_idx = int(round((clip_end_play - start_time) * samplerate))
                    num_frames = end_idx - start_idx

                    if num_frames <= 0:
                        continue

                    t_start_in_clip = c.trim_start + (clip_start_play - c.timeline_pos)
                    t_end_in_clip = c.trim_start + (clip_end_play - c.timeline_pos)

                    if c.is_audio_only:
                        audio_obj = c.media
                    else:
                        audio_obj = c.media.audio

                    if audio_obj is not None:
                        try:
                            # Generate an array of timestamps perfectly matched to our chunk size
                            t_array = np.linspace(t_start_in_clip, t_end_in_clip, num_frames, endpoint=False)
                            # Protect against EOF audio read errors
                            t_array = np.clip(t_array, 0, audio_obj.duration - 0.05)
                            frames = audio_obj.get_frame(t_array)
                            
                            # Expand mono tracks to stereo
                            if frames.ndim == 1:
                                frames = np.column_stack((frames, frames))
                                
                            if frames.shape[0] == num_frames:
                                # Apply volume
                                frames = frames * c.volume
                                
                                # Apply audio fades for preview
                                if c.fade_in or c.fade_out:
                                    timeline_t_array = np.linspace(clip_start_play, clip_end_play, num_frames, endpoint=False)
                                    if c.fade_in:
                                        fade_in_mult = np.clip(timeline_t_array - c.timeline_pos, 0, 1.0)
                                        frames = frames * fade_in_mult[:, np.newaxis]
                                    if c.fade_out:
                                        clip_end_pos = c.timeline_pos + c.duration
                                        fade_out_mult = np.clip(clip_end_pos - timeline_t_array, 0, 1.0)
                                        frames = frames * fade_out_mult[:, np.newaxis]

                                audio_data[start_idx:end_idx] += frames
                        except Exception:
                            pass

                # Cap output volume to avoid clipping distortion 
                audio_data = np.clip(audio_data, -1.0, 1.0)
                try:
                    # Write to stream (blocks thread automatically to maintain real-time pace)
                    stream.write(audio_data)
                except Exception:
                    pass

                # Only advance if UI hasn't explicitly scrubbed/jumped the playhead while we were blocking
                if self.audio_playhead == start_time:
                    self.audio_playhead += (chunk_frames / samplerate)
            else:
                time.sleep(0.01)

    def _apply_preview_image(self, img, is_empty=False):
        if not self.root.winfo_exists():
            return
        try:
            # Generate the label photo directly from our consistently-created PIL Image
            photo = ctk.CTkImage(light_image=img, size=img.size)
            
            # Show empty instructions on pure black space, otherwise leave blank to not overlap video
            txt = "Black Screen / No Clip\n(Drag & Drop Media Here)" if is_empty else ""
            
            self.preview_label.configure(image=photo, text=txt)
            self.preview_label._image = photo # Keep reference
        except Exception:
            pass

    # --- Export Logic ---
    def detect_best_encoder(self):
        import platform
        import subprocess
        system = platform.system().lower()
        if system == "darwin":
            return "Mac GPU (VideoToolbox)"
        elif system == "windows":
            try:
                # Query Windows for graphics card names using PowerShell (wmic is deprecated in Windows 11)
                output = subprocess.check_output('powershell -command "Get-CimInstance -ClassName Win32_VideoController | Select-Object -ExpandProperty Name"', shell=True, text=True).lower()
                if "nvidia" in output:
                    return "NVIDIA GPU (NVENC)"
                elif "amd" in output or "radeon" in output:
                    return "AMD GPU (AMF)"
                elif "intel" in output:
                    return "Intel GPU (QSV)"
            except Exception:
                pass
        return "CPU Default (Standard)"

    def open_export_settings(self):
        if not self.clips:
            messagebox.showinfo("Export", "No clips on the timeline to export.")
            return

        self.export_win = ctk.CTkToplevel(self.root)
        self.export_win.title("Export Settings")
        self.export_win.geometry("400x380")
        self.export_win.transient(self.root)
        self.export_win.grab_set()

        raw_w, raw_h = self.get_video_size()
        def_fps = self.get_fps()

        ctk.CTkLabel(self.export_win, text="Resolution Width:").pack(pady=(15, 0))
        w_entry = ctk.CTkEntry(self.export_win)
        w_entry.insert(0, str(raw_w))
        w_entry.pack()

        ctk.CTkLabel(self.export_win, text="Resolution Height:").pack(pady=(10, 0))
        h_entry = ctk.CTkEntry(self.export_win)
        h_entry.insert(0, str(raw_h))
        h_entry.pack()

        ctk.CTkLabel(self.export_win, text="Framerate (FPS):").pack(pady=(10, 0))
        fps_entry = ctk.CTkEntry(self.export_win)
        fps_entry.insert(0, str(def_fps))
        fps_entry.pack()

        ctk.CTkLabel(self.export_win, text="Render Engine:").pack(pady=(10, 0))
        best_engine = self.detect_best_encoder()
        self.selected_codec_name.set(best_engine)
        codec_menu = ctk.CTkOptionMenu(self.export_win, variable=self.selected_codec_name, values=list(self.codec_mapping.keys()), width=200)
        codec_menu.pack()

        def on_continue():
            try:
                target_w = int(w_entry.get())
                target_h = int(h_entry.get())
                target_fps = float(fps_entry.get())
            except ValueError:
                messagebox.showerror("Invalid Input", "Please enter valid numbers for resolution and framerate.", parent=self.export_win)
                return
            self.export_win.destroy()
            self.do_export(target_w, target_h, target_fps)

        ctk.CTkButton(self.export_win, text="Continue...", command=on_continue, fg_color="#2E7D32", hover_color="#388E3C").pack(pady=25)

    def do_export(self, target_w, target_h, target_fps):
        output_path = filedialog.asksaveasfilename(
            title="Export Video",
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4")]
        )
        
        if not output_path:
            return

        self.progress_win = ctk.CTkToplevel(self.root)
        self.progress_win.title("Exporting Video")
        self.progress_win.geometry("450x150")
        self.progress_win.transient(self.root)
        self.progress_win.grab_set()

        ctk.CTkLabel(self.progress_win, text="Rendering your video, please wait...", font=("Arial", 14)).pack(pady=10)
        
        self.progress_bar = ctk.CTkProgressBar(self.progress_win)
        self.progress_bar.pack(fill="x", padx=20, pady=10)
        self.progress_bar.set(0) # In CTk, progress bars are 0.0 to 1.0
        
        self.progress_lbl = ctk.CTkLabel(self.progress_win, text="Initializing FFmpeg Pipeline...", font=("Arial", 12))
        self.progress_lbl.pack()

        self.root.config(cursor="wait")
        
        selected_name = self.selected_codec_name.get()
        actual_codec = self.codec_mapping.get(selected_name, "libx264")
        
        thread = threading.Thread(target=self._process_export, args=(output_path, actual_codec, target_w, target_h, target_fps), daemon=True)
        thread.start()

    def update_progress_ui(self, pct, step_name):
        # Convert 0-100 percentage to 0.0-1.0 for CustomTkinter
        self.root.after(0, lambda p=pct: self.progress_bar.set(p / 100.0))
        self.root.after(0, lambda p=pct, s=step_name: self.progress_lbl.configure(text=f"Rendering {s} progress: {int(p)}%"))

    def _fmt_time(self, seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(',', '.')

    def _process_export(self, output_path, codec_id, target_w, target_h, fps):
        try:
            import subprocess
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

            master_w = (target_w // 2) * 2
            master_h = (target_h // 2) * 2
            
            min_pos = min(c.timeline_pos for c in self.clips)
            max_duration = max((c.timeline_pos + c.duration for c in self.clips)) - min_pos

            bg_v = ffmpeg.input(f'color=c=black:s={master_w}x{master_h}:r={fps}', f='lavfi')
            bg_a = ffmpeg.input('anullsrc=channel_layout=stereo:sample_rate=44100', f='lavfi')

            overlays = bg_v
            audios = [bg_a]

            sorted_clips = sorted(self.clips, key=lambda c: c.track, reverse=True)

            for c in sorted_clips:
                is_img = getattr(c, 'is_image', False)
                if is_img:
                    in_file = ffmpeg.input(c.filepath, loop=1, framerate=fps)
                else:
                    in_file = ffmpeg.input(c.filepath)
                
                shifted_pos = c.timeline_pos - min_pos
                shifted_pos_ms = int(shifted_pos * 1000)
                
                if not c.is_audio_only:
                    if is_img:
                        # Images don't have natural duration bounds, simply output what is visible on timeline
                        v = in_file.video.filter('trim', start=0, end=self._fmt_time(c.duration))
                    else:
                        v = in_file.video.filter('trim', start=self._fmt_time(c.trim_start), end=self._fmt_time(c.trim_end))
                    
                    v = v.filter('setpts', 'PTS-STARTPTS')
                    v = v.filter('scale', master_w, master_h)
                    v = v.filter('setpts', f'PTS-STARTPTS+({shifted_pos_ms}/1000)/TB')
                    
                    overlays = ffmpeg.overlay(overlays, v, eof_action='pass')

                if not c.is_muted:
                    if c.is_audio_only:
                        has_audio = True
                    elif is_img:
                        has_audio = False
                    else:
                        has_audio = c.media.audio is not None

                    if has_audio:
                        a = in_file.audio.filter('atrim', start=self._fmt_time(c.trim_start), end=self._fmt_time(c.trim_end))
                        a = a.filter('asetpts', 'PTS-STARTPTS')
                        
                        # Apply fades and volume natively in FFmpeg based on zero-started timestamps
                        if c.fade_in:
                            a = a.filter('afade', type='in', start_time=0, duration=1.0)
                        if c.fade_out:
                            a = a.filter('afade', type='out', start_time=c.duration - 1.0, duration=1.0)
                        if c.volume != 1.0:
                            a = a.filter('volume', str(c.volume))
                            
                        a = a.filter('adelay', f'{shifted_pos_ms}|{shifted_pos_ms}')
                        audios.append(a)

            if len(audios) > 1:
                out_a = ffmpeg.filter(audios, 'amix', inputs=len(audios), duration='first')
            else:
                out_a = audios[0]

            output_kwargs = {
                'vcodec': codec_id,
                'acodec': 'aac',
                'r': fps,
                't': self._fmt_time(max_duration)
            }
            
            if codec_id == 'libx264':
                output_kwargs['preset'] = 'fast'
                output_kwargs['crf'] = '22'  # Ensures x264 also has high visual quality
                thread_count = getattr(os, 'cpu_count', lambda: 4)()
                if thread_count:
                    output_kwargs['threads'] = thread_count
            elif codec_id == 'h264_nvenc':
                # FIX: Explicitly set Constant Quality metrics for NVIDIA GPU
                output_kwargs['cq'] = '20'   # Constant quality (lower is better, 20 is great)
                output_kwargs['rc'] = 'vbr'  # Use Variable Bitrate to hit the CQ target
                output_kwargs['b:v'] = '0'   # Necessary fallback for NVENC to respect CQ setting
            elif codec_id in ['h264_amf', 'h264_qsv', 'h264_videotoolbox']:
                # FIX: Set a safe high bitrate fallback for other GPU encoders
                output_kwargs['b:v'] = '15M'

            out = ffmpeg.output(overlays, out_a, output_path, **output_kwargs)
            cmd = [ffmpeg_exe, '-y'] + out.get_args()

            process = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace'
            )

            time_pattern = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
            for line in process.stderr:
                match = time_pattern.search(line)
                if match:
                    h, m, s = map(float, match.groups())
                    current_time = h * 3600 + m * 60 + s
                    pct = (current_time / max_duration) * 100
                    self.update_progress_ui(min(pct, 100), "Video")

            process.wait()

            if process.returncode == 0:
                self.root.after(0, lambda: self._export_complete(True, output_path))
            else:
                self.root.after(0, lambda: self._export_complete(False, "FFmpeg failed. The console/terminal may hold more info."))
                
        except Exception as e:
            # Capture the error string immediately before the exception block cleans up 'e'
            err_msg = str(e)
            self.root.after(0, lambda msg=err_msg: self._export_complete(False, msg))

    def _export_complete(self, success, msg):
        self.root.config(cursor="")
        if hasattr(self, 'progress_win') and self.progress_win.winfo_exists():
            self.progress_win.destroy()
            
        if success:
            messagebox.showinfo("Success", f"Video exported successfully to:\n{msg}")
        else:
            fallback_hint = "\n\nHint: It seems your system does not support the selected GPU encoder. Try selecting 'CPU Default' instead." if "CPU" not in self.selected_codec_name.get() else ""
            messagebox.showerror("Export Error", f"An error occurred during export:\n{msg}{fallback_hint}")

if __name__ == "__main__":
    if HAS_DND:
        root = CTk_DnD()
    else:
        root = ctk.CTk()
        
    app = VisualVideoEditor(root)

    # Check for files passed in via "Open with..." context menu
    import sys
    if len(sys.argv) > 1:
        def load_initial_files():
            for filepath in sys.argv[1:]:
                if os.path.exists(filepath):
                    app.add_media_from_path(filepath)
        # Delay slightly so the UI finishes drawing before the heavy loading freezes it
        root.after(200, load_initial_files)

    root.mainloop()