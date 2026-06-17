import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import os
import time
import re

# --- Dependency Checks ---
try:
    # Try MoviePy v2+ imports first
    from moviepy import VideoFileClip, CompositeVideoClip, ColorClip
except ImportError:
    try:
        # Fallback to MoviePy v1.x imports
        from moviepy.editor import VideoFileClip, CompositeVideoClip, ColorClip
    except ImportError:
        messagebox.showerror("Missing Dependency", "Please install moviepy: pip install moviepy\n(Also make sure your virtual environment is activated!)")
        exit()

try:
    from PIL import Image, ImageTk
except ImportError:
    messagebox.showerror("Missing Dependency", "Please install Pillow: pip install Pillow")
    exit()

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False
    print("Notice: tkinterdnd2 not installed. Drag and drop will be disabled.")
    print("To enable drag and drop: pip install tkinterdnd2")

try:
    import ffmpeg
    import imageio_ffmpeg
except ImportError:
    messagebox.showerror("Missing Dependency", "Please install ffmpeg-python:\npip install ffmpeg-python imageio-ffmpeg")
    exit()

class TimelineClip:
    """Represents a segment of video on the timeline."""
    def __init__(self, filepath, video_clip, timeline_pos):
        self.filepath = filepath
        self.video = video_clip
        self.trim_start = 0.0
        self.trim_end = video_clip.duration
        self.timeline_pos = timeline_pos
        self.track = 0  # Default to top track
        self.selected = False
        self.is_muted = False

    @property
    def duration(self):
        return self.trim_end - self.trim_start

class VisualVideoEditor:
    def __init__(self, root):
        self.root = root
        self.root.title("Visual Clip Editor")
        self.root.geometry("950x850")
        self.root.minsize(850, 750)
        
        # --- Drag and Drop Setup ---
        if HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind('<<Drop>>', self.handle_drop)
        
        # --- State Variables ---
        self.clips = []
        self.pixels_per_sec = 20.0  # Zoom level
        self.playhead_time = 0.0
        
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
        
        # Hardware Acceleration Mapping
        self.codec_mapping = {
            "CPU Default (Standard)": "libx264",
            "NVIDIA GPU (NVENC)": "h264_nvenc",
            "AMD GPU (AMF)": "h264_amf",
            "Mac GPU (VideoToolbox)": "h264_videotoolbox",
            "Intel GPU (QSV)": "h264_qsv"
        }
        self.selected_codec_name = tk.StringVar(value="CPU Default (Standard)")

        self.create_widgets()
        self.setup_keybinds()
        self.draw_timeline()

    def create_widgets(self):
        # --- Toolbar ---
        toolbar = tk.Frame(self.root, bd=1, relief="raised")
        toolbar.pack(side="top", fill="x")
        
        tk.Button(toolbar, text="Add Video", command=self.add_video).pack(side="left", padx=5, pady=5)
        tk.Button(toolbar, text="Delete Selected", command=self.delete_selected).pack(side="left", padx=5, pady=5)
        
        tk.Button(toolbar, text="Export Video", command=self.export_video, bg="#4CAF50", fg="white", font=("Arial", 9, "bold")).pack(side="right", padx=5, pady=5)
        
        codec_menu = tk.OptionMenu(toolbar, self.selected_codec_name, *self.codec_mapping.keys())
        codec_menu.pack(side="right", padx=5, pady=5)
        tk.Label(toolbar, text="Render Engine:").pack(side="right")
        
        # --- Timeline Area ---
        timeline_container = tk.Frame(self.root, height=290, bg="#2b2b2b")
        timeline_container.pack(side="bottom", fill="x")
        timeline_container.pack_propagate(False)
        
        zoom_frame = tk.Frame(timeline_container, bg="#2b2b2b")
        zoom_frame.pack(side="top", fill="x", padx=5, pady=2)
        tk.Label(zoom_frame, text="Timeline Tools:", bg="#2b2b2b", fg="white").pack(side="left")
        tk.Button(zoom_frame, text="Zoom In (+)", command=self.zoom_in, width=10).pack(side="right", padx=2)
        tk.Button(zoom_frame, text="Zoom Out (-)", command=self.zoom_out, width=10).pack(side="right", padx=2)

        playback_frame = tk.Frame(self.root, bd=0, pady=5)
        playback_frame.pack(side="bottom", fill="x")
        
        btn_container = tk.Frame(playback_frame)
        btn_container.pack(expand=True)
        
        tk.Button(btn_container, text="⏪ 5s", command=self.skip_back, width=6).pack(side="left", padx=5)
        self.play_button = tk.Button(btn_container, text="Play", command=self.toggle_play, width=10, bg="#4CAF50", fg="white")
        self.play_button.pack(side="left", padx=5)
        tk.Button(btn_container, text="5s ⏩", command=self.skip_forward, width=6).pack(side="left", padx=5)

        self.h_scroll = tk.Scrollbar(timeline_container, orient="horizontal")
        self.h_scroll.pack(side="bottom", fill="x")
        
        self.canvas = tk.Canvas(timeline_container, bg="#3c3f41", xscrollcommand=self.h_scroll.set, height=250)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.h_scroll.config(command=self.canvas.xview)

        # --- Preview Area ---
        self.preview_frame = tk.Frame(self.root, bg="black")
        self.preview_frame.pack(side="top", fill="both", expand=True)
        self.preview_frame.pack_propagate(False)
        
        self.preview_label = tk.Label(self.preview_frame, bg="black", text="No Video Loaded\n(Drag & Drop Video Here)", fg="white")
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        
        self.canvas.bind("<Button-1>", self.on_left_click)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_release)
        self.canvas.bind("<Button-3>", self.on_right_click)
        
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-4>", self.on_mousewheel)
        self.canvas.bind("<Button-5>", self.on_mousewheel)
        
        self.root.bind("<Configure>", lambda e: self.update_preview_image())

    def setup_keybinds(self):
        self.root.bind("<Delete>", lambda e: self.delete_selected())
        self.root.bind("<BackSpace>", lambda e: self.delete_selected())
        self.root.bind("<space>", self.on_space_pressed)
        self.root.bind("<Left>", lambda e: self.skip_back())
        self.root.bind("<Right>", lambda e: self.skip_forward())

    def on_space_pressed(self, event):
        self.toggle_play()
        return "break"

    def handle_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        for filepath in files:
            if any(filepath.lower().endswith(ext) for ext in ['.mp4', '.avi', '.mov', '.mkv']):
                self.add_video_from_path(filepath)

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

    # --- Playback Logic ---
    def toggle_play(self):
        if not self.clips:
            return
            
        self.is_playing = not self.is_playing
        if self.is_playing:
            self.play_button.config(text="Pause", bg="#f44336")
            self.last_play_time = time.time()
            self.play_loop()
        else:
            self.play_button.config(text="Play", bg="#4CAF50")

    def play_loop(self):
        if not self.is_playing:
            return

        current_time = time.time()
        delta = current_time - self.last_play_time
        self.last_play_time = current_time

        self.playhead_time += delta
        max_time = max((c.timeline_pos + c.duration for c in self.clips)) if self.clips else 0

        if self.playhead_time >= max_time and max_time > 0:
            self.playhead_time = max_time
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
        self.draw_timeline()
        self.update_preview_image()

    def skip_forward(self):
        self.playhead_time += 5.0
        max_time = max((c.timeline_pos + c.duration for c in self.clips)) if self.clips else 0
        if max_time > 0:
            self.playhead_time = min(self.playhead_time, max_time)
        self.draw_timeline()
        self.update_preview_image()

    # --- Interaction Logic ---
    def get_time_from_x(self, x):
        canvas_x = self.canvas.canvasx(x)
        return max(0, canvas_x / self.pixels_per_sec)

    def get_track_from_y(self, y):
        if y < 30: return 0
        t = int((y - 30) / (self.track_height + self.track_spacing))
        return max(0, min(self.num_tracks - 1, t))

    def get_clip_at_mouse(self, x, y):
        time_sec = self.get_time_from_x(x)
        track_idx = self.get_track_from_y(y)
        for clip in reversed(self.clips):
            if clip.track == track_idx and clip.timeline_pos <= time_sec <= (clip.timeline_pos + clip.duration):
                return clip
        return None

    def would_overlap(self, time_start, duration, track_idx, ignore_clip=None):
        """Returns True if the specified time frame intersects with an existing clip on the track."""
        time_end = time_start + duration
        for c in self.clips:
            if c != ignore_clip and c.track == track_idx:
                if time_start < c.timeline_pos + c.duration and time_end > c.timeline_pos:
                    return True
        return False

    def on_left_click(self, event):
        if self.is_playing:
            self.toggle_play()

        click_time = self.get_time_from_x(event.x)
        if event.y < 30:
            self.playhead_time = click_time
        
        for c in self.clips:
            c.selected = False
            
        clicked_clip = self.get_clip_at_mouse(event.x, event.y) if event.y >= 30 else None
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
            self.drag_mode = 'scrub' if event.y < 30 else None

        self.draw_timeline()
        self.update_preview_image()

    def on_mouse_drag(self, event):
        current_time = self.get_time_from_x(event.x)
        
        if self.drag_clip and self.drag_mode in ['move', 'trim_left', 'trim_right']:
            clip = self.drag_clip
            ctrl_held = (event.state & 0x0004) or (event.state & 0x0008) # Windows Ctrl or Mac Cmd
            snap_threshold = 15.0 / self.pixels_per_sec
            
            if self.drag_mode == 'move':
                new_track = self.get_track_from_y(event.y)
                new_time = max(0, current_time - self.drag_offset)
                
                # --- Moving Snap Logic ---
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

                # --- Moving Collision Logic ---
                if not self.would_overlap(new_time, clip.duration, new_track, ignore_clip=clip):
                    clip.timeline_pos = new_time
                    clip.track = new_track
                else:
                    # Push it against the obstacle to prevent merging
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
                
                # --- Trim Snap Logic ---
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
                
                # --- Trim Left Collision Logic ---
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
                
                # --- Trim Snap Logic ---
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
                
                # --- Trim Right Collision Logic ---
                max_pos = float('inf')
                for c in self.clips:
                    if c != clip and c.track == clip.track and c.timeline_pos >= clip.timeline_pos:
                        max_pos = min(max_pos, c.timeline_pos)
                        
                if clip.timeline_pos + new_duration > max_pos:
                    new_duration = max_pos - clip.timeline_pos
                    new_trim_end = clip.trim_start + new_duration
                
                if clip.trim_start + 0.5 < new_trim_end <= clip.video.duration:
                    clip.trim_end = new_trim_end

            self.draw_timeline()
            if time.time() - self.last_preview_time > 0.05:
                self.update_preview_image(fast_mode=True)
                self.last_preview_time = time.time()

        elif self.drag_mode == 'scrub':
            self.playhead_time = current_time
            self.draw_timeline()
            if time.time() - self.last_preview_time > 0.05:
                self.update_preview_image(fast_mode=True)
                self.last_preview_time = time.time()

    def on_mouse_release(self, event):
        self.drag_mode = None
        self.drag_clip = None
        self.update_preview_image()

    def on_right_click(self, event):
        if self.is_playing:
            self.toggle_play()

        click_time = self.get_time_from_x(event.x)
        clip_to_split = self.get_clip_at_mouse(event.x, event.y) if event.y >= 30 else None
        
        if not clip_to_split:
            return
            
        for c in self.clips:
            c.selected = False
        clip_to_split.selected = True
        
        self.draw_timeline()
        self.update_preview_image()
        
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Cut Here", command=lambda: self.split_clip(clip_to_split, click_time))
        
        mute_label = "Unmute Audio" if clip_to_split.is_muted else "Mute Audio"
        menu.add_command(label=mute_label, command=lambda: self.toggle_mute(clip_to_split))
        
        menu.add_separator()
        menu.add_command(label="Delete", command=self.delete_selected)
        menu.post(event.x_root, event.y_root)

    def toggle_mute(self, clip):
        clip.is_muted = not clip.is_muted
        self.draw_timeline()

    def split_clip(self, clip_to_split, click_time):
        split_point_in_clip = click_time - clip_to_split.timeline_pos
        split_point_in_source = clip_to_split.trim_start + split_point_in_clip
        
        if split_point_in_clip < 0.5 or (clip_to_split.duration - split_point_in_clip) < 0.5:
            return

        new_clip = TimelineClip(clip_to_split.filepath, VideoFileClip(clip_to_split.filepath), click_time)
        new_clip.trim_start = split_point_in_source
        new_clip.trim_end = clip_to_split.trim_end
        new_clip.is_muted = clip_to_split.is_muted
        new_clip.track = clip_to_split.track  # Stay on the same track
        
        clip_to_split.trim_end = split_point_in_source
        
        self.clips.append(new_clip)
        self.draw_timeline()
        self.update_preview_image()

    def delete_selected(self):
        self.clips = [c for c in self.clips if not c.selected]
        self.draw_timeline()
        self.update_preview_image()

    # --- Video & Drawing Logic ---
    def add_video(self):
        filepath = filedialog.askopenfilename(filetypes=[("Video Files", "*.mp4 *.avi *.mov *.mkv")])
        if filepath:
            self.add_video_from_path(filepath)

    def add_video_from_path(self, filepath):
        try:
            video = VideoFileClip(filepath)
            start_pos = 0.0
            
            # Default to Track 0 and find end
            track0_clips = [c for c in self.clips if c.track == 0]
            if track0_clips:
                last_clip = max(track0_clips, key=lambda c: c.timeline_pos + c.duration)
                start_pos = last_clip.timeline_pos + last_clip.duration
                
            new_clip = TimelineClip(filepath, video, start_pos)
            new_clip.track = 0
            self.clips.append(new_clip)
            
            self.draw_timeline()
            if len(self.clips) == 1:
                self.playhead_time = 0.0
                self.update_preview_image()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load video:\n{e}")

    def draw_timeline(self):
        self.canvas.delete("all")
        
        max_time = 60.0
        if self.clips:
            furthest_edge = max((c.timeline_pos + c.duration for c in self.clips))
            max_time = max(max_time, furthest_edge + 30.0)
            
        canvas_width = max_time * self.pixels_per_sec
        canvas_height = 30 + self.num_tracks * (self.track_height + self.track_spacing)
        self.canvas.config(scrollregion=(0, 0, canvas_width, canvas_height))
        
        # Draw Timeline Hash Marks
        for i in range(0, int(max_time), 5):
            x = i * self.pixels_per_sec
            self.canvas.create_line(x, 0, x, 20, fill="gray")
            self.canvas.create_text(x + 2, 10, text=f"{i}s", fill="gray", anchor="w", font=("Arial", 8))
            
        # Draw Track Lanes
        for t in range(self.num_tracks):
            y1 = 30 + t * (self.track_height + self.track_spacing)
            y2 = y1 + self.track_height
            self.canvas.create_rectangle(0, y1, canvas_width, y2, fill="#36393b", outline="")
            self.canvas.create_text(5, y1 + 5, text=f"Track {t+1}", fill="#777777", anchor="nw", font=("Arial", 7, "bold"))
            
        for clip in self.clips:
            x1 = clip.timeline_pos * self.pixels_per_sec
            x2 = (clip.timeline_pos + clip.duration) * self.pixels_per_sec
            
            y1 = 30 + clip.track * (self.track_height + self.track_spacing)
            y2 = y1 + self.track_height
            
            color = "#4a86e8" if not clip.selected else "#6fa8dc"
            outline = "#ffffff" if clip.selected else "#111111"
            
            self.canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline=outline, width=2)
            
            handle_width = 6
            self.canvas.create_rectangle(x1, y1, x1 + handle_width, y2, fill="#295496", outline="")
            self.canvas.create_rectangle(x2 - handle_width, y1, x2, y2, fill="#295496", outline="")
            
            filename = os.path.basename(clip.filepath)
            self.canvas.create_text(x1 + 10, y1 + 15, text=filename, fill="white", anchor="w", font=("Arial", 9))
            
            time_txt = f"{clip.trim_start:.1f}s - {clip.trim_end:.1f}s"
            self.canvas.create_text(x1 + 10, y1 + 35, text=time_txt, fill="#dddddd", anchor="w", font=("Arial", 8))
            
            audio_icon = "🔇" if clip.is_muted else "🔊"
            icon_x_pos = max(x1 + 30, x2 - 20)
            self.canvas.create_text(icon_x_pos, y1 + 25, text=audio_icon, fill="white", font=("Arial", 14))

        # Draw Playhead covering all tracks
        px = self.playhead_time * self.pixels_per_sec
        self.canvas.create_line(px, 0, px, canvas_height, fill="red", width=2)
        self.canvas.create_polygon(px-5, 0, px+5, 0, px, 10, fill="red")

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
                # Find all clips matching the playhead
                clips_at_time = [c for c in self.clips if c.timeline_pos <= playhead_time <= c.timeline_pos + c.duration]
                
                if not clips_at_time:
                    self.root.after(0, self._apply_preview_image, None)
                    continue
                
                # Sort descending to draw the highest track index (bottom visual layer) first
                clips_at_time.sort(key=lambda c: c.track, reverse=True)
                
                # Create base compositing canvas
                base_img = Image.new('RGB', target_size, (0, 0, 0))
                resample_filter = Image.Resampling.NEAREST if fast_mode else Image.Resampling.BILINEAR
                
                for clip in clips_at_time:
                    time_in_source = clip.trim_start + (playhead_time - clip.timeline_pos)
                    time_in_source = max(0, min(time_in_source, clip.video.duration))
                    
                    frame = clip.video.get_frame(time_in_source)
                    img = Image.fromarray(frame)
                    
                    # Match FFmpeg scale behavior by pasting the frame directly on top
                    img = img.resize(target_size, resample_filter)
                    base_img.paste(img, (0, 0))
                    
                self.root.after(0, self._apply_preview_image, base_img)
            except Exception:
                pass

    def _apply_preview_image(self, img):
        if not self.root.winfo_exists():
            return
        try:
            if img is None:
                self.preview_label.config(image='', text="Black Screen / No Clip\n(Drag & Drop Video Here)")
                self.preview_label.image = None
                return
                
            photo = ImageTk.PhotoImage(image=img)
            self.preview_label.config(image=photo, text="")
            self.preview_label.image = photo
        except Exception:
            pass

    # --- Export Logic ---
    def export_video(self):
        if not self.clips:
            messagebox.showinfo("Export", "No clips on the timeline to export.")
            return
            
        output_path = filedialog.asksaveasfilename(
            title="Export Video",
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4")]
        )
        
        if not output_path:
            return

        self.progress_win = tk.Toplevel(self.root)
        self.progress_win.title("Exporting Video")
        self.progress_win.geometry("400x120")
        self.progress_win.transient(self.root)
        self.progress_win.grab_set()

        tk.Label(self.progress_win, text="Rendering your video, please wait...", font=("Arial", 10)).pack(pady=10)
        
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(self.progress_win, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", padx=20, pady=5)
        
        self.progress_lbl = tk.Label(self.progress_win, text="Initializing FFmpeg Pipeline...", font=("Arial", 9))
        self.progress_lbl.pack()

        self.root.config(cursor="wait")
        
        selected_name = self.selected_codec_name.get()
        actual_codec = self.codec_mapping.get(selected_name, "libx264")
        
        thread = threading.Thread(target=self._process_export, args=(output_path, actual_codec), daemon=True)
        thread.start()

    def update_progress_ui(self, pct, step_name):
        self.root.after(0, lambda p=pct: self.progress_var.set(p))
        self.root.after(0, lambda p=pct, s=step_name: self.progress_lbl.config(text=f"Rendering {s} progress: {int(p)}%"))

    def _fmt_time(self, seconds):
        """Formats seconds into HH:MM:SS.mmm to completely bypass FFmpeg locale parsing bugs."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(',', '.')

    def _process_export(self, output_path, codec_id):
        try:
            import subprocess
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

            raw_master_size = self.clips[0].video.size
            master_w = (raw_master_size[0] // 2) * 2
            master_h = (raw_master_size[1] // 2) * 2
            fps = self.clips[0].video.fps
            
            # --- BUG FIX: Remove leading blank space ---
            # Find the true start time in case the user left empty space at the beginning
            min_pos = min(c.timeline_pos for c in self.clips)
            max_duration = max((c.timeline_pos + c.duration for c in self.clips)) - min_pos

            # Remove 't' from lavfi inputs; infinite streams prevent EOF leaks. Output '-t' handles the cutoff.
            bg_v = ffmpeg.input(f'color=c=black:s={master_w}x{master_h}:r={fps}', f='lavfi')
            bg_a = ffmpeg.input('anullsrc=channel_layout=stereo:sample_rate=44100', f='lavfi')

            overlays = bg_v
            audios = [bg_a]

            # Sorted descending by track index so highest tracks (0, 1) overlay the lower tracks (2, 3) last
            sorted_clips = sorted(self.clips, key=lambda c: c.track, reverse=True)

            for c in sorted_clips:
                in_file = ffmpeg.input(c.filepath)
                
                # Shift position to remove the leading empty space
                shifted_pos = c.timeline_pos - min_pos
                shifted_pos_ms = int(shifted_pos * 1000) # Use integer math to prevent decimal issues
                
                v = in_file.video.filter('trim', start=self._fmt_time(c.trim_start), end=self._fmt_time(c.trim_end))
                v = v.filter('setpts', 'PTS-STARTPTS')
                v = v.filter('scale', master_w, master_h)
                v = v.filter('setpts', f'PTS-STARTPTS+({shifted_pos_ms}/1000)/TB')
                
                overlays = ffmpeg.overlay(overlays, v, eof_action='pass')

                if not c.is_muted and c.video.audio is not None:
                    a = in_file.audio.filter('atrim', start=self._fmt_time(c.trim_start), end=self._fmt_time(c.trim_end))
                    a = a.filter('asetpts', 'PTS-STARTPTS')
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
                't': self._fmt_time(max_duration)  # Force explicit HH:MM:SS format to bypass locale bugs
            }
            
            if codec_id == 'libx264':
                output_kwargs['preset'] = 'fast'
                thread_count = getattr(os, 'cpu_count', lambda: 4)()
                if thread_count:
                    output_kwargs['threads'] = thread_count

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
            self.root.after(0, lambda: self._export_complete(False, str(e)))

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
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
        
    app = VisualVideoEditor(root)
    root.mainloop()