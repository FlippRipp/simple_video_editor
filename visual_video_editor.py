import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import os
import time

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

class TimelineClip:
    """Represents a segment of video on the timeline."""
    def __init__(self, filepath, video_clip, timeline_pos):
        self.filepath = filepath
        self.video = video_clip
        self.trim_start = 0.0
        self.trim_end = video_clip.duration
        self.timeline_pos = timeline_pos
        self.selected = False
        self.is_muted = False  # New property for tracking audio state

    @property
    def duration(self):
        return self.trim_end - self.trim_start

class VisualVideoEditor:
    def __init__(self, root):
        self.root = root
        self.root.title("Visual Clip Editor")
        self.root.geometry("950x750")
        self.root.minsize(850, 650)
        
        # --- State Variables ---
        self.clips = []
        self.pixels_per_sec = 20  # Zoom level
        self.playhead_time = 0.0
        
        # Interaction state
        self.drag_mode = None  # 'move', 'trim_left', 'trim_right'
        self.drag_clip = None
        self.drag_offset = 0.0
        self.last_preview_time = -1
        
        # Playback state
        self.is_playing = False
        self.last_play_time = 0.0
        
        # --- Background Preview Thread ---
        # Decouples FFMPEG decoding from the UI so the app never freezes
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
        self.draw_timeline()

    def create_widgets(self):
        # --- Toolbar ---
        toolbar = tk.Frame(self.root, bd=1, relief="raised")
        toolbar.pack(side="top", fill="x")
        
        tk.Button(toolbar, text="Add Video", command=self.add_video).pack(side="left", padx=5, pady=5)
        tk.Button(toolbar, text="Delete Selected", command=self.delete_selected).pack(side="left", padx=5, pady=5)
        
        tk.Button(toolbar, text="Export Video", command=self.export_video, bg="#4CAF50", fg="white", font=("Arial", 9, "bold")).pack(side="right", padx=5, pady=5)
        
        # Codec dropdown menu
        codec_menu = tk.OptionMenu(toolbar, self.selected_codec_name, *self.codec_mapping.keys())
        codec_menu.pack(side="right", padx=5, pady=5)
        tk.Label(toolbar, text="Render Engine:").pack(side="right")
        
        # --- Timeline Area ---
        # Packed FIRST to the bottom so it never gets pushed off screen
        timeline_container = tk.Frame(self.root, height=200, bg="#2b2b2b")
        timeline_container.pack(side="bottom", fill="x")
        timeline_container.pack_propagate(False)
        
        # --- Playback Controls ---
        # Packed SECOND to the bottom
        playback_frame = tk.Frame(self.root, bd=0, pady=5)
        playback_frame.pack(side="bottom", fill="x")
        
        btn_container = tk.Frame(playback_frame)
        btn_container.pack(expand=True)
        
        tk.Button(btn_container, text="⏪ 5s", command=self.skip_back, width=6).pack(side="left", padx=5)
        self.play_button = tk.Button(btn_container, text="Play", command=self.toggle_play, width=10, bg="#4CAF50", fg="white")
        self.play_button.pack(side="left", padx=5)
        tk.Button(btn_container, text="5s ⏩", command=self.skip_forward, width=6).pack(side="left", padx=5)

        # Scrollbar for timeline
        self.h_scroll = tk.Scrollbar(timeline_container, orient="horizontal")
        self.h_scroll.pack(side="bottom", fill="x")
        
        self.canvas = tk.Canvas(timeline_container, bg="#3c3f41", xscrollcommand=self.h_scroll.set, height=180)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.h_scroll.config(command=self.canvas.xview)

        # --- Preview Area ---
        # Packed LAST with expand=True so it only takes leftover space
        self.preview_frame = tk.Frame(self.root, bg="black")
        self.preview_frame.pack(side="top", fill="both", expand=True)
        self.preview_frame.pack_propagate(False) # Prevents the image from forcing the window to resize
        
        self.preview_label = tk.Label(self.preview_frame, bg="black", text="No Video Loaded", fg="white")
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center") # Centers the video, creating black bars naturally
        
        # Timeline Event Bindings
        self.canvas.bind("<Button-1>", self.on_left_click)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_release)
        self.canvas.bind("<Button-3>", self.on_right_click) # Right click to open menu
        
        # Keyboard Bindings for Deletion
        self.root.bind("<Delete>", lambda e: self.delete_selected())
        self.root.bind("<BackSpace>", lambda e: self.delete_selected())
        
        # Update canvas size periodically
        self.root.bind("<Configure>", lambda e: self.update_preview_image())

    # --- Playback Logic ---
    def toggle_play(self):
        if not self.clips:
            return  # Prevent playback if no clips are loaded
            
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

        # Determine max timeline time to stop playback automatically
        max_time = 0
        if self.clips:
            max_time = max((c.timeline_pos + c.duration for c in self.clips))

        if self.playhead_time >= max_time and max_time > 0:
            self.playhead_time = max_time
            self.draw_timeline()
            self.update_preview_image()
            self.toggle_play() # Pause at the end
            return

        self.draw_timeline()
        # Fast mode uses faster resampling so it doesn't lag too much during playback
        self.update_preview_image(fast_mode=True)

        if self.is_playing:
            # Re-run after ~33 milliseconds (approximating 30 fps)
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
        """Convert canvas X coordinate to timeline time in seconds."""
        # Adjust for scroll position
        canvas_x = self.canvas.canvasx(x)
        return max(0, canvas_x / self.pixels_per_sec)

    def get_clip_at_time(self, time_sec):
        # Reverse list to select the one "on top" if they overlap
        for clip in reversed(self.clips):
            if clip.timeline_pos <= time_sec <= (clip.timeline_pos + clip.duration):
                return clip
        return None

    def on_left_click(self, event):
        # Pause playback if running
        if self.is_playing:
            self.toggle_play()

        click_time = self.get_time_from_x(event.x)
        
        # Only update playhead if clicking the top ruler area
        if event.y < 30:
            self.playhead_time = click_time
        
        # Deselect all
        for c in self.clips:
            c.selected = False
            
        # Only interact with clips if clicking below the time ruler (y >= 30)
        if event.y >= 30:
            clicked_clip = self.get_clip_at_time(click_time)
        else:
            clicked_clip = None
            
        self.drag_clip = clicked_clip
        
        if clicked_clip:
            clicked_clip.selected = True
            # Determine if we clicked an edge (trimming) or the body (moving)
            edge_threshold = 5.0 / self.pixels_per_sec # 5 pixels in seconds
            
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
            if event.y < 30:
                self.drag_mode = 'scrub'
            else:
                self.drag_mode = None

        self.draw_timeline()
        self.update_preview_image()

    def on_mouse_drag(self, event):
        current_time = self.get_time_from_x(event.x)
        
        if self.drag_clip and self.drag_mode in ['move', 'trim_left', 'trim_right']:
            clip = self.drag_clip
            
            if self.drag_mode == 'move':
                # Move the clip
                new_pos = current_time - self.drag_offset
                clip.timeline_pos = max(0, new_pos)
                
            elif self.drag_mode == 'trim_left':
                # Calculate how much we moved
                delta = current_time - clip.timeline_pos
                new_trim_start = clip.trim_start + delta
                
                # Constrain trim limits
                if 0 <= new_trim_start < clip.trim_end - 0.5: # Minimum 0.5 sec clip
                    clip.trim_start = new_trim_start
                    clip.timeline_pos = current_time
                    
            elif self.drag_mode == 'trim_right':
                delta = current_time - (clip.timeline_pos + clip.duration)
                new_trim_end = clip.trim_end + delta
                
                # Constrain trim limits
                if clip.trim_start + 0.5 < new_trim_end <= clip.video.duration:
                    clip.trim_end = new_trim_end

            self.draw_timeline()
            
            # Throttle preview updates while dragging
            if time.time() - self.last_preview_time > 0.05:
                self.update_preview_image(fast_mode=True)
                self.last_preview_time = time.time()
        elif self.drag_mode == 'scrub':
            # Just scrubbing the playhead
            self.playhead_time = current_time
            self.draw_timeline()
            if time.time() - self.last_preview_time > 0.05:
                self.update_preview_image(fast_mode=True)
                self.last_preview_time = time.time()

    def on_mouse_release(self, event):
        self.drag_mode = None
        self.drag_clip = None
        self.update_preview_image() # Final precise update

    def on_right_click(self, event):
        """Shows a context menu to split, mute, or delete the clip."""
        # Pause playback if running
        if self.is_playing:
            self.toggle_play()

        click_time = self.get_time_from_x(event.x)
        clip_to_split = self.get_clip_at_time(click_time)
        
        if not clip_to_split:
            return
            
        # Select the clip that was right-clicked
        for c in self.clips:
            c.selected = False
        clip_to_split.selected = True
        
        self.draw_timeline()
        self.update_preview_image()
        
        # Create and display the context menu
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Cut Here", command=lambda: self.split_clip(clip_to_split, click_time))
        
        # Audio Mute/Unmute Toggle
        mute_label = "Unmute Audio" if clip_to_split.is_muted else "Mute Audio"
        menu.add_command(label=mute_label, command=lambda: self.toggle_mute(clip_to_split))
        
        menu.add_separator()
        menu.add_command(label="Delete", command=self.delete_selected)
        menu.post(event.x_root, event.y_root)

    def toggle_mute(self, clip):
        """Toggles the mute state of the selected clip."""
        clip.is_muted = not clip.is_muted
        self.draw_timeline()

    def split_clip(self, clip_to_split, click_time):
        """Logic for splitting a clip at a specific time."""
        # Calculate exactly where inside the source video we are splitting
        split_point_in_clip = click_time - clip_to_split.timeline_pos
        split_point_in_source = clip_to_split.trim_start + split_point_in_clip
        
        # Don't split if it's too close to the edges
        if split_point_in_clip < 0.5 or (clip_to_split.duration - split_point_in_clip) < 0.5:
            return

        # Create the second half
        new_clip = TimelineClip(clip_to_split.filepath, VideoFileClip(clip_to_split.filepath), click_time)
        new_clip.trim_start = split_point_in_source
        new_clip.trim_end = clip_to_split.trim_end
        new_clip.is_muted = clip_to_split.is_muted  # Carry over the mute state
        
        # Update the first half
        clip_to_split.trim_end = split_point_in_source
        
        self.clips.append(new_clip)
        self.draw_timeline()
        self.update_preview_image()

    def delete_selected(self):
        """Deletes any currently selected clips."""
        self.clips = [c for c in self.clips if not c.selected]
        self.draw_timeline()
        self.update_preview_image()

    # --- Video & Drawing Logic ---
    def add_video(self):
        filepath = filedialog.askopenfilename(filetypes=[("Video Files", "*.mp4 *.avi *.mov *.mkv")])
        if not filepath:
            return
            
        try:
            video = VideoFileClip(filepath)
            
            # Place it at the end of the current timeline
            start_pos = 0.0
            if self.clips:
                last_clip = max(self.clips, key=lambda c: c.timeline_pos + c.duration)
                start_pos = last_clip.timeline_pos + last_clip.duration
                
            new_clip = TimelineClip(filepath, video, start_pos)
            self.clips.append(new_clip)
            
            self.draw_timeline()
            if len(self.clips) == 1:
                self.playhead_time = 0.0
                self.update_preview_image()
                
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load video:\n{e}")

    def draw_timeline(self):
        self.canvas.delete("all")
        
        # Calculate timeline width
        max_time = 60.0 # Minimum 1 minute timeline
        if self.clips:
            furthest_edge = max((c.timeline_pos + c.duration for c in self.clips))
            max_time = max(max_time, furthest_edge + 30.0) # Add 30s buffer
            
        canvas_width = max_time * self.pixels_per_sec
        self.canvas.config(scrollregion=(0, 0, canvas_width, 180))
        
        # Draw Time Markers
        for i in range(0, int(max_time), 5):
            x = i * self.pixels_per_sec
            self.canvas.create_line(x, 0, x, 20, fill="gray")
            self.canvas.create_text(x + 2, 10, text=f"{i}s", fill="gray", anchor="w", font=("Arial", 8))
            
        # Draw Clips
        track_y1 = 30
        track_y2 = 110
        
        for clip in self.clips:
            x1 = clip.timeline_pos * self.pixels_per_sec
            x2 = (clip.timeline_pos + clip.duration) * self.pixels_per_sec
            
            color = "#4a86e8" if not clip.selected else "#6fa8dc"
            outline = "#ffffff" if clip.selected else "#111111"
            
            # Draw Main Clip Body
            self.canvas.create_rectangle(x1, track_y1, x2, track_y2, fill=color, outline=outline, width=2)
            
            # Draw Trim Handles (darker edges)
            handle_width = 6
            self.canvas.create_rectangle(x1, track_y1, x1 + handle_width, track_y2, fill="#295496", outline="")
            self.canvas.create_rectangle(x2 - handle_width, track_y1, x2, track_y2, fill="#295496", outline="")
            
            # Clip Name Text
            filename = os.path.basename(clip.filepath)
            self.canvas.create_text(x1 + 10, track_y1 + 15, text=filename, fill="white", anchor="w", font=("Arial", 9))
            
            # Show trimmed times
            time_txt = f"{clip.trim_start:.1f}s - {clip.trim_end:.1f}s"
            self.canvas.create_text(x1 + 10, track_y1 + 35, text=time_txt, fill="#dddddd", anchor="w", font=("Arial", 8))
            
            # Draw Audio Indicator Icon
            audio_icon = "🔇" if clip.is_muted else "🔊"
            # Placed near the right edge of the clip, just before the trim handle
            icon_x_pos = max(x1 + 30, x2 - 20) # Keep it from jumping over text if clip is too small
            self.canvas.create_text(icon_x_pos, track_y1 + 25, text=audio_icon, fill="white", font=("Arial", 14))

        # Draw Playhead
        px = self.playhead_time * self.pixels_per_sec
        self.canvas.create_line(px, 0, px, 180, fill="red", width=2)
        self.canvas.create_polygon(px-5, 0, px+5, 0, px, 10, fill="red")

    def update_preview_image(self, fast_mode=False):
        """Signals the background thread to grab the frame so the UI never lags."""
        clip_at_playhead = self.get_clip_at_time(self.playhead_time)
        
        if not clip_at_playhead:
            self.preview_label.config(image='', text="Black Screen / No Clip")
            self.preview_label.image = None
            return

        target_width = self.preview_frame.winfo_width()
        target_height = self.preview_frame.winfo_height()
        
        if target_width < 10 or target_height < 10:
            return

        # Hand off the heavy decoding to the background thread
        self.preview_request = (clip_at_playhead, self.playhead_time, (target_width, target_height), fast_mode)
        self.preview_event.set()

    def _preview_worker(self):
        """Runs in the background to extract FFMPEG frames without freezing the UI."""
        while True:
            self.preview_event.wait()
            self.preview_event.clear()
            
            if not self.preview_request:
                continue
                
            clip, playhead_time, target_size, fast_mode = self.preview_request
            
            try:
                time_in_source = clip.trim_start + (playhead_time - clip.timeline_pos)
                time_in_source = max(0, min(time_in_source, clip.video.duration))
                
                # HEAVY FFMPEG DECODING OPERATION:
                # Running this here ensures the Tkinter UI drag/play loops never stall.
                frame = clip.video.get_frame(time_in_source)
                img = Image.fromarray(frame)
                
                resample_filter = Image.Resampling.NEAREST if fast_mode else Image.Resampling.BILINEAR
                img.thumbnail(target_size, resample_filter)
                
                # Safely send the processed image back to the main UI thread
                self.root.after(0, self._apply_preview_image, img)
            except Exception as e:
                pass

    def _apply_preview_image(self, img):
        """Called by the main thread to safely update the Tkinter label with the new frame."""
        if not self.root.winfo_exists():
            return
        try:
            photo = ImageTk.PhotoImage(image=img)
            self.preview_label.config(image=photo, text="")
            self.preview_label.image = photo # Keep reference to avoid garbage collection
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

        # Disable UI elements
        self.root.config(cursor="wait")
        
        # Determine chosen codec
        selected_name = self.selected_codec_name.get()
        actual_codec = self.codec_mapping.get(selected_name, "libx264")
        
        # Run export in background
        thread = threading.Thread(target=self._process_export, args=(output_path, actual_codec), daemon=True)
        thread.start()

    def _process_export(self, output_path, codec_id):
        try:
            render_clips = []
            
            # Prepare all clips according to their trim and timeline positions
            for c in self.clips:
                # Compatibility for MoviePy v1 vs v2
                if hasattr(c.video, "subclipped"):
                    sub = c.video.subclipped(c.trim_start, c.trim_end)
                    sub = sub.with_start(c.timeline_pos)
                else:
                    sub = c.video.subclip(c.trim_start, c.trim_end)
                    sub = sub.set_start(c.timeline_pos)
                
                # Apply mute if toggled
                if c.is_muted:
                    sub = sub.without_audio()
                    
                render_clips.append(sub)
            
            # Calculate total duration
            max_duration = max((c.timeline_pos + c.duration for c in self.clips))
            
            # Create a base black clip to ensure the video has a consistent size and background
            # We use the size of the first clip as the master resolution
            master_size = self.clips[0].video.size
            bg_clip = ColorClip(size=master_size, color=(0,0,0), duration=max_duration)
            
            render_clips.insert(0, bg_clip)
            
            # Composite them together
            final_video = CompositeVideoClip(render_clips)
            
            # Render
            print(f"Starting render using {codec_id}... please wait.")
            final_video.write_videofile(
                output_path, 
                codec=codec_id, 
                audio_codec="aac",
                fps=self.clips[0].video.fps
            )
            
            self.root.after(0, lambda: self._export_complete(True, output_path))
            
        except Exception as e:
            self.root.after(0, lambda: self._export_complete(False, str(e)))

    def _export_complete(self, success, msg):
        self.root.config(cursor="")
        if success:
            messagebox.showinfo("Success", f"Video exported successfully to:\n{msg}")
        else:
            # Provide a helpful hint if a GPU codec fails
            fallback_hint = "\n\nHint: It seems your system does not support the selected GPU encoder. Try selecting 'CPU Default' instead." if "CPU" not in self.selected_codec_name.get() else ""
            messagebox.showerror("Export Error", f"An error occurred during export:\n{msg}{fallback_hint}")

if __name__ == "__main__":
    root = tk.Tk()
    app = VisualVideoEditor(root)
    root.mainloop()