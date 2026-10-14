import os
import sys
import re
import time
import shutil
import tempfile
import subprocess
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QComboBox, QSpinBox,
    QProgressBar, QFileDialog, QGroupBox, QRadioButton, QFormLayout,
    QTextEdit, QMessageBox, QStackedWidget, QButtonGroup, QCheckBox
)

def to_long_path(p):
    """Wandelt Windows-Kurzpfade (z.B. HAUPTK~1) in lange Pfade um."""
    if not p:
        return ""
    try:
        return str(Path(p).resolve())
    except Exception:
        return os.path.normpath(p)

# ----------------------------------------------------------------------
# HINTERGRUND-THREAD: KETTEN-VERARBEITUNG MIT MEILENSTEINEN
# ----------------------------------------------------------------------
class FFmpegWorker(QThread):
    progress_current = pyqtSignal(int)
    progress_overall = pyqtSignal(int, int)
    finished_signal = pyqtSignal(str)
    log_signal = pyqtSignal(str)

    def __init__(self, ffmpeg_exe, input_path, output_dir, temp_dir, ffmpeg_args, extension, total_iterations, custom_tag="", keep_milestones=False, milestone_interval=10):
        super().__init__()
        self.ffmpeg_exe = to_long_path(ffmpeg_exe)
        self.input_path = to_long_path(input_path)
        self.output_dir = to_long_path(output_dir)
        self.temp_dir = to_long_path(temp_dir)
        self.ffmpeg_args = ffmpeg_args
        self.extension = extension
        self.total_iterations = total_iterations
        self.custom_tag = custom_tag.strip()
        self.keep_milestones = keep_milestones
        self.milestone_interval = milestone_interval
        self._stop_requested = False

    def stop_gracefully(self):
        self._stop_requested = True

    def run(self):
        current_input = self.input_path
        temp_dir = self.temp_dir
        out_dir = self.output_dir
        ffmpeg_bin = shutil.which(self.ffmpeg_exe) or self.ffmpeg_exe
        
        created_temp_files = []
        completed_iterations = 0
        total_duration_us = None

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        tag_str = f"_{self.custom_tag}" if self.custom_tag else ""
        orig_filename = os.path.basename(self.input_path)
        filename_no_ext, _ = os.path.splitext(orig_filename)

        saved_milestones = []

        for i in range(1, self.total_iterations + 1):
            self.progress_overall.emit(i, self.total_iterations)
            self.progress_current.emit(0)

            temp_output = to_long_path(os.path.join(temp_dir, f"lab_temp_gen_{i}{self.extension}"))
            cmd = [ffmpeg_bin, "-y", "-i", current_input, "-progress", "pipe:1"] + self.ffmpeg_args + [temp_output]
            self.log_signal.emit(f"Starte Durchlauf {i}/{self.total_iterations}...")

            try:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8", errors="replace"
                )
            except Exception as e:
                self.finished_signal.emit(f"Fehler beim Starten von FFmpeg:\n{e}")
                return

            ffmpeg_log = []
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break

                line = line.strip()
                if line:
                    ffmpeg_log.append(line)

                if total_duration_us is None and "Duration:" in line:
                    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", line)
                    if match:
                        h, m, s = match.groups()
                        total_duration_us = int((int(h) * 3600 + int(m) * 60 + float(s)) * 1_000_000)

                if line.startswith("out_time_us=") and total_duration_us:
                    try:
                        out_time_us = int(line.split("=")[1])
                        pct = int((out_time_us / total_duration_us) * 100)
                        self.progress_current.emit(min(100, max(0, pct)))
                    except ValueError:
                        pass

            if process.stdout:
                process.stdout.close()
            process.wait()

            if process.returncode != 0 or not os.path.isfile(temp_output) or os.path.getsize(temp_output) == 0:
                error_snippet = "\n".join(ffmpeg_log[-12:]) if ffmpeg_log else "Keine FFmpeg-Ausgabe verfügbar."
                self.finished_signal.emit(f"Fehler in Durchlauf {i}/{self.total_iterations}!\n\nFFmpeg-Auszug:\n{error_snippet}")
                return

            self.progress_current.emit(100)
            created_temp_files.append(temp_output)
            completed_iterations = i

            # Meilenstein im Zielordner ablegen (falls aktiviert und Intervall erreicht)
            if self.keep_milestones and (i % self.milestone_interval == 0) and (i < self.total_iterations):
                milestone_filename = f"{filename_no_ext}{tag_str}_gen{i}_{timestamp}{self.extension}"
                milestone_path = to_long_path(os.path.join(out_dir, milestone_filename))
                try:
                    os.makedirs(out_dir, exist_ok=True)
                    shutil.copyfile(temp_output, milestone_path)
                    saved_milestones.append(milestone_filename)
                    self.log_signal.emit(f"Meilenstein Gen {i} gespeichert: {milestone_filename}")
                except Exception as e:
                    self.log_signal.emit(f"Fehler beim Speichern von Meilenstein Gen {i}: {e}")

            # Löschen der Vor-Zwischendatei erst NACH Erfolgsbestätigung des aktuellen Durchlaufs
            if len(created_temp_files) > 1:
                prev_file = created_temp_files[-2]
                if os.path.isfile(prev_file):
                    try:
                        os.remove(prev_file)
                    except Exception as e:
                        self.log_signal.emit(f"Konnte alte Temp-Datei nicht löschen: {e}")

            current_input = temp_output

            if self._stop_requested:
                self.log_signal.emit(f"Stopp nach Durchlauf {i} ausgeführt.")
                break

        # FINALE DATEI MIT EINDEUTIGEM NAMEN SPEICHERN
        if completed_iterations > 0 and os.path.isfile(current_input):
            final_filename = f"{filename_no_ext}{tag_str}_gen{completed_iterations}_{timestamp}{self.extension}"
            final_output_path = to_long_path(os.path.join(out_dir, final_filename))

            time.sleep(0.5)
            saved = False
            last_error = ""
            
            for attempt in range(1, 4):
                try:
                    os.makedirs(out_dir, exist_ok=True)
                    if os.path.exists(final_output_path):
                        try: os.remove(final_output_path)
                        except Exception: pass
                    
                    shutil.copyfile(current_input, final_output_path)
                    saved = True
                    break
                except Exception as e:
                    last_error = str(e)
                    time.sleep(0.5)

            if saved:
                ms_info = ""
                if saved_milestones:
                    ms_info = f"\n\nZusätzlich {len(saved_milestones)} Meilenstein(e) gespeichert:\n" + "\n".join([f"• {m}" for m in saved_milestones])
                final_msg = f"Prozess erfolgreich beendet ({completed_iterations} Durchläufe)!\n\nFinale Datei:\n{final_output_path}{ms_info}"
                if os.path.isfile(current_input):
                    try: os.remove(current_input)
                    except Exception: pass
            else:
                final_msg = (
                    f"Fehler beim Speichern nach '{final_output_path}':\n{last_error}\n\n"
                    f"Die fertig gerenderte Datei liegt im Temp-Ordner:\n{current_input}"
                )
        else:
            final_msg = "Prozess abgebrochen oder keine Datei erzeugt."

        self.finished_signal.emit(final_msg)


# ----------------------------------------------------------------------
# HAUPTFENSTER DER BENUTZEROBERFLÄCHE
# ----------------------------------------------------------------------
class VideoLabApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FFmpeg Generation Loss & Codec Lab")
        self.resize(900, 800)
        
        self.worker = None

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # 1. DATEI- & PFAD-AUSWAHL
        file_box = QGroupBox("Pfade & Dateien")
        file_layout = QFormLayout(file_box)

        self.btn_open_video = QPushButton("Durchsuchen...")
        self.btn_open_video.clicked.connect(self.select_video_file)
        self.txt_file_path = QLineEdit()
        self.txt_file_path.setPlaceholderText("Videodatei auswählen...")
        video_row = QHBoxLayout()
        video_row.addWidget(self.txt_file_path)
        video_row.addWidget(self.btn_open_video)

        self.btn_open_target = QPushButton("Durchsuchen...")
        self.btn_open_target.clicked.connect(self.select_target_dir)
        self.txt_target_path = QLineEdit()
        self.txt_target_path.setPlaceholderText("Zielordner auswählen...")
        target_row = QHBoxLayout()
        target_row.addWidget(self.txt_target_path)
        target_row.addWidget(self.btn_open_target)

        self.btn_open_ffmpeg = QPushButton("Durchsuchen...")
        self.btn_open_ffmpeg.clicked.connect(self.select_ffmpeg_exe)
        self.txt_ffmpeg_path = QLineEdit("ffmpeg")
        self.txt_ffmpeg_path.editingFinished.connect(self.update_hardware_options)
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.txt_ffmpeg_path)
        ffmpeg_row.addWidget(self.btn_open_ffmpeg)

        self.btn_open_temp = QPushButton("Durchsuchen...")
        self.btn_open_temp.clicked.connect(self.select_temp_dir)
        self.txt_temp_path = QLineEdit(to_long_path(tempfile.gettempdir()))
        temp_row = QHBoxLayout()
        temp_row.addWidget(self.txt_temp_path)
        temp_row.addWidget(self.btn_open_temp)

        file_layout.addRow("Eingangs-Video:", video_row)
        file_layout.addRow("Ziel-Ordner:", target_row)
        file_layout.addRow("FFmpeg Pfad:", ffmpeg_row)
        file_layout.addRow("Temp-Ordner (RAM-Disk):", temp_row)
        main_layout.addWidget(file_box)

        # 2. MEILENSTEIN-EINSTELLUNGEN
        opts_box = QGroupBox("Optionen für Zwischenstände (Meilensteine)")
        opts_layout = QHBoxLayout(opts_box)

        self.chk_keep_milestones = QCheckBox("Meilensteine im Zielordner speichern")
        self.lbl_milestone_step = QLabel("Alle N Durchläufe:")
        self.spn_milestone_step = QSpinBox()
        self.spn_milestone_step.setRange(1, 500)
        self.spn_milestone_step.setValue(10)
        self.spn_milestone_step.setEnabled(False)

        self.chk_keep_milestones.toggled.connect(self.spn_milestone_step.setEnabled)

        opts_layout.addWidget(self.chk_keep_milestones)
        opts_layout.addWidget(self.lbl_milestone_step)
        opts_layout.addWidget(self.spn_milestone_step)
        opts_layout.addStretch()
        main_layout.addWidget(opts_box)

        # 3. NAVIGATIONSLEISTE
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(2)

        self.btn_simpel = QPushButton("Simpel")
        self.btn_standard = QPushButton("Standard")
        self.btn_expert = QPushButton("Expert")

        button_style = """
            QPushButton {
                background-color: #f0f0f0;
                border: 1px solid #b0b0b0;
                font-weight: normal;
                padding: 4px 8px;
                color: #222222;
            }
            QPushButton:hover {
                background-color: #e0e0e0;
            }
            QPushButton:checked {
                background-color: #888888;
                color: white;
                border: 2px inset #555555;
            }
        """

        self.btn_group = QButtonGroup(self)
        self.btn_group.setExclusive(True)

        for idx, btn in enumerate([self.btn_simpel, self.btn_standard, self.btn_expert]):
            btn.setCheckable(True)
            btn.setStyleSheet(button_style)
            nav_layout.addWidget(btn)
            self.btn_group.addButton(btn, idx)

        self.btn_simpel.setChecked(True)
        main_layout.addLayout(nav_layout)

        # 4. SEITEN-CONTAINER
        self.pages = QStackedWidget()
        
        self.tab_simpel = QWidget()
        self.tab_standard = QWidget()
        self.tab_expert = QWidget()

        self.pages.addWidget(self.tab_simpel)
        self.pages.addWidget(self.tab_standard)
        self.pages.addWidget(self.tab_expert)
        main_layout.addWidget(self.pages)

        self.btn_group.idClicked.connect(self.pages.setCurrentIndex)

        self._setup_tab_simpel()
        self._setup_tab_standard()
        self._setup_tab_expert()

        # 5. FORTSCHRITTSANZEIGE
        bottom_box = QGroupBox("Fortschritt & Steuerung")
        bottom_layout = QVBoxLayout(bottom_box)

        lbl_curr = QLabel("Aktueller Durchlauf:")
        self.pbar_current = QProgressBar()
        self.pbar_current.setValue(0)
        
        lbl_overall = QLabel("Gesamtdurchlauf:")
        self.pbar_overall = QProgressBar()
        self.pbar_overall.setValue(0)

        bottom_layout.addWidget(lbl_curr)
        bottom_layout.addWidget(self.pbar_current)
        bottom_layout.addWidget(lbl_overall)
        bottom_layout.addWidget(self.pbar_overall)

        btn_layout = QHBoxLayout()
        self.btn_start = QPushButton("Testreihe Starten")
        self.btn_start.clicked.connect(self.start_process)
        self.btn_stop = QPushButton("Nach aktuellem Durchlauf stoppen")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_process)

        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_stop)
        bottom_layout.addLayout(btn_layout)

        main_layout.addWidget(bottom_box)

        self.update_hardware_options()

    def _test_gpu_codec(self, ffmpeg_bin, codec_name):
        cmd = [
            ffmpeg_bin, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=25",
            "-frames:v", "1",
            "-c:v", codec_name,
            "-f", "null", "-"
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
            return res.returncode == 0
        except Exception:
            return False

    def detect_available_encoders(self):
        ffmpeg_exe = to_long_path(self.txt_ffmpeg_path.text().strip())
        ffmpeg_bin = shutil.which(ffmpeg_exe) or (ffmpeg_exe if os.path.exists(ffmpeg_exe) else None)
        
        if not ffmpeg_bin:
            return {"nvenc": False, "qsv": False, "amf": False}

        return {
            "nvenc": self._test_gpu_codec(ffmpeg_bin, "h264_nvenc"),
            "qsv": self._test_gpu_codec(ffmpeg_bin, "h264_qsv"),
            "amf": self._test_gpu_codec(ffmpeg_bin, "h264_amf")
        }

    def _apply_hw_states_to_combo(self, combo, hw_states):
        combo.blockSignals(True)
        model = combo.model()

        model.item(0).setEnabled(True)
        combo.setItemText(0, "CPU (Software)")

        has_nv = hw_states["nvenc"]
        model.item(1).setEnabled(has_nv)
        combo.setItemText(1, "Nvidia GPU (NVENC)" if has_nv else "Nvidia GPU (Nicht verfügbar)")

        has_qsv = hw_states["qsv"]
        model.item(2).setEnabled(has_qsv)
        combo.setItemText(2, "Intel GPU (QuickSync)" if has_qsv else "Intel GPU (Nicht verfügbar)")

        has_amf = hw_states["amf"]
        model.item(3).setEnabled(has_amf)
        combo.setItemText(3, "AMD GPU (AMF)" if has_amf else "AMD GPU (Nicht verfügbar)")

        if not model.item(combo.currentIndex()).isEnabled():
            combo.setCurrentIndex(0)

        combo.blockSignals(False)

    def update_hardware_options(self):
        hw_states = self.detect_available_encoders()
        self._apply_hw_states_to_combo(self.cbo_simpel_hw, hw_states)
        self._apply_hw_states_to_combo(self.cbo_std_hw, hw_states)
        self._apply_hw_states_to_combo(self.cbo_exp_hw, hw_states)
        self._update_simpel_info()

    def _create_container_combo(self):
        cbo = QComboBox()
        cbo.addItems([
            "MP4 (H.264 – Maximal kompatibel)",
            "MP4 / MKV (AV1 – Hochmoderne Kompression)",
            "MKV (H.265 / HEVC – Hohe Effizienz)"
        ])
        return cbo

    def _create_hw_combo(self):
        cbo = QComboBox()
        cbo.addItems([
            "CPU (Software)",
            "Nvidia GPU (NVENC)",
            "Intel GPU (QuickSync)",
            "AMD GPU (AMF)"
        ])
        return cbo

    def _create_loops_spinbox(self):
        spn = QSpinBox()
        spn.setRange(1, 500)
        spn.setValue(10)
        return spn

    def _setup_tab_simpel(self):
        layout = QHBoxLayout(self.tab_simpel)
        form = QFormLayout()

        self.cbo_simpel_container = self._create_container_combo()
        self.cbo_simpel_preset = QComboBox()
        self.cbo_simpel_preset.addItems([
            "Hohe Qualität (Wenig Verlust)",
            "Ausgewogen (Standard)",
            "Starke Kompression (Hoher Verlust)"
        ])
        self.cbo_simpel_preset.currentIndexChanged.connect(self._update_simpel_info)

        self.txt_simpel_tag = QLineEdit()
        self.txt_simpel_tag.setPlaceholderText("Optionaler Tag (z.B. test1)")

        self.spn_simpel_loops = self._create_loops_spinbox()
        self.cbo_simpel_hw = self._create_hw_combo()
        self.cbo_simpel_hw.currentIndexChanged.connect(self._update_simpel_info)
        
        self.lbl_simpel_hw_info = QLabel()
        self.lbl_simpel_hw_info.setWordWrap(True)

        hw_row_layout = QHBoxLayout()
        hw_row_layout.addWidget(self.cbo_simpel_hw)
        hw_row_layout.addWidget(self.lbl_simpel_hw_info, stretch=1)

        form.addRow("Container & Codec:", self.cbo_simpel_container)
        form.addRow("Qualitätsvoreinstellung:", self.cbo_simpel_preset)
        form.addRow("Eigener Tag / Suffix:", self.txt_simpel_tag)
        form.addRow("Durchläufe:", self.spn_simpel_loops)
        form.addRow("Encoding-Typ:", hw_row_layout)

        self.lbl_simpel_qual_info = QLabel()
        self.lbl_simpel_qual_info.setWordWrap(True)

        layout.addLayout(form, stretch=2)
        layout.addWidget(self.lbl_simpel_qual_info, stretch=1)
        self._update_simpel_info()

    def _update_simpel_info(self):
        if self.cbo_simpel_hw.currentIndex() == 0:
            hw_text = "Software Encoding: hohe Präzision, langsamer."
        else:
            hw_text = "Hardware Encoding: enorm schnell, nutzspezifische Chip-Logik."
        self.lbl_simpel_hw_info.setText(f"<i>{hw_text}</i>")

        qual_desc = [
            "<b>Geringe Kompression:</b> Ideal um zu prüfen, wie stabil das Bild bei minimalen Eingriffen über mehrere Generationen bleibt.",
            "<b>Standard-Kompressionsrate:</b> Typische Einstellungen. Erste Bildfehler zeigen sich meist nach wenigen Durchläufen.",
            "<b>Aggressive Datenreduktion:</b> Erzeugt sehr schnell sichtbare Block-Artefakte, Unschärfe und Farbverschiebungen."
        ]
        self.lbl_simpel_qual_info.setText(qual_desc[self.cbo_simpel_preset.currentIndex()])

    def _setup_tab_standard(self):
        form = QFormLayout(self.tab_standard)

        self.cbo_std_container = self._create_container_combo()
        self.cbo_std_hw = self._create_hw_combo()
        
        self.cbo_std_preset = QComboBox()
        self.cbo_std_preset.addItems(["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"])
        self.cbo_std_preset.setCurrentText("medium")

        self.rad_std_crf = QRadioButton("CRF Nutzen (Constant Rate Factor)")
        self.rad_std_crf.setChecked(True)
        self.spn_std_crf = QSpinBox()
        self.spn_std_crf.setRange(0, 51)
        self.spn_std_crf.setValue(23)

        self.rad_std_bitrate = QRadioButton("Feste Bitrate (in Mbit/s)")
        self.spn_std_bitrate = QSpinBox()
        self.spn_std_bitrate.setRange(1, 100)
        self.spn_std_bitrate.setValue(5)

        self.spn_std_gop = QSpinBox()
        self.spn_std_gop.setRange(1, 1000)
        self.spn_std_gop.setValue(250)

        self.spn_std_bframes = QSpinBox()
        self.spn_std_bframes.setRange(0, 16)
        self.spn_std_bframes.setValue(3)

        self.txt_std_tag = QLineEdit()
        self.txt_std_tag.setPlaceholderText("Optionaler Tag (z.B. std_crf23)")

        self.spn_std_loops = self._create_loops_spinbox()

        form.addRow("Container & Codec:", self.cbo_std_container)
        form.addRow("Preset:", self.cbo_std_preset)
        form.addRow(self.rad_std_crf, self.spn_std_crf)
        form.addRow(self.rad_std_bitrate, self.spn_std_bitrate)
        form.addRow("Keyframe-Abstand (GOP):", self.spn_std_gop)
        form.addRow("B-Frames:", self.spn_std_bframes)
        form.addRow("Eigener Tag / Suffix:", self.txt_std_tag)
        form.addRow("Durchläufe:", self.spn_std_loops)
        form.addRow("Encoding-Typ:", self.cbo_std_hw)

    def _setup_tab_expert(self):
        form = QFormLayout(self.tab_expert)

        self.cbo_exp_container = self._create_container_combo()
        self.cbo_exp_hw = self._create_hw_combo()
        self.cbo_exp_pix_fmt = QComboBox()
        self.cbo_exp_pix_fmt.addItems(["yuv420p", "yuv422p", "yuv444p", "rgb24"])

        self.txt_exp_me = QLineEdit()
        self.txt_exp_me.setPlaceholderText("z.B. hex, umh, esa")
        
        self.spn_exp_subq = QSpinBox()
        self.spn_exp_subq.setRange(1, 11)
        self.spn_exp_subq.setValue(7)

        self.txt_exp_custom = QTextEdit()
        self.txt_exp_custom.setPlaceholderText("z.B. -x264-params me=umh:merange=24")

        self.txt_exp_tag = QLineEdit()
        self.txt_exp_tag.setPlaceholderText("Optionaler Tag (z.B. exp_yuv422p)")

        self.spn_exp_loops = self._create_loops_spinbox()

        form.addRow("Container & Codec:", self.cbo_exp_container)
        form.addRow("Farbraum / Subsampling:", self.cbo_exp_pix_fmt)
        form.addRow("ME Method (Motion Est.):", self.txt_exp_me)
        form.addRow("Subpel Refinement (subq):", self.spn_exp_subq)
        form.addRow("Eigene FFmpeg-Flags:", self.txt_exp_custom)
        form.addRow("Eigener Tag / Suffix:", self.txt_exp_tag)
        form.addRow("Durchläufe:", self.spn_exp_loops)
        form.addRow("Encoding-Typ:", self.cbo_exp_hw)

    def _get_encoder_codec(self, container_index, hw_index):
        if container_index == 0:   # H.264
            codecs = ["libx264", "h264_nvenc", "h264_qsv", "h264_amf"]
        elif container_index == 1: # AV1
            codecs = ["libsvtav1", "av1_nvenc", "av1_qsv", "av1_amf"]
        else:                      # HEVC
            codecs = ["libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf"]
        return codecs[hw_index]

    def get_active_config(self):
        current_page = self.pages.currentIndex()
        args = []
        extension = ".mp4"
        loops = 10
        tag = ""

        if current_page == 0:   # SIMPEL
            loops = self.spn_simpel_loops.value()
            container_idx = self.cbo_simpel_container.currentIndex()
            hw_idx = self.cbo_simpel_hw.currentIndex()
            codec = self._get_encoder_codec(container_idx, hw_idx)
            extension = ".mkv" if container_idx == 2 else ".mp4"
            
            idx = self.cbo_simpel_preset.currentIndex()
            val = "18" if idx == 0 else ("23" if idx == 1 else "30")
            preset_slug = "hohe_qualitaet" if idx == 0 else ("ausgewogen" if idx == 1 else "starke_kompression")
            
            user_tag = self.txt_simpel_tag.text().strip()
            tag = f"simpel_{preset_slug}" + (f"_{user_tag}" if user_tag else "")

            if hw_idx == 0:   # CPU
                args = ["-c:v", codec, "-crf", val, "-c:a", "copy"]
            elif hw_idx == 1: # NVENC
                args = ["-c:v", codec, "-preset", "p5", "-rc:v", "vbr", "-cq", val, "-c:a", "copy"]
            else:             # Sonstige GPU
                args = ["-c:v", codec, "-rc", "vbr", "-cq", val, "-c:a", "copy"]

        elif current_page == 1: # STANDARD
            loops = self.spn_std_loops.value()
            container_idx = self.cbo_std_container.currentIndex()
            hw_idx = self.cbo_std_hw.currentIndex()
            codec = self._get_encoder_codec(container_idx, hw_idx)
            extension = ".mkv" if container_idx == 2 else ".mp4"
            
            user_tag = self.txt_std_tag.text().strip()
            quality_tag = f"crf{self.spn_std_crf.value()}" if self.rad_std_crf.isChecked() else f"{self.spn_std_bitrate.value()}M"
            tag = f"std_{quality_tag}" + (f"_{user_tag}" if user_tag else "")

            if hw_idx == 1:
                args = ["-c:v", codec, "-preset", "p5"]
            else:
                args = ["-c:v", codec, "-preset", self.cbo_std_preset.currentText()]

            if self.rad_std_crf.isChecked():
                val = str(self.spn_std_crf.value())
                if hw_idx == 0:
                    args.extend(["-crf", val])
                else:
                    args.extend(["-rc:v", "vbr", "-cq", val])
            else:
                args.extend(["-b:v", f"{self.spn_std_bitrate.value()}M"])
                
            args.extend(["-g", str(self.spn_std_gop.value()), "-bf", str(self.spn_std_bframes.value()), "-c:a", "copy"])

        elif current_page == 2: # EXPERT
            loops = self.spn_exp_loops.value()
            container_idx = self.cbo_exp_container.currentIndex()
            hw_idx = self.cbo_exp_hw.currentIndex()
            codec = self._get_encoder_codec(container_idx, hw_idx)
            extension = ".mkv" if container_idx == 2 else ".mp4"
            
            user_tag = self.txt_exp_tag.text().strip()
            pix = self.cbo_exp_pix_fmt.currentText()
            tag = f"exp_{pix}" + (f"_{user_tag}" if user_tag else "")

            args = ["-c:v", codec, "-pix_fmt", pix]
            if self.txt_exp_me.text():
                args.extend(["-me_method", self.txt_exp_me.text()])
            args.extend(["-subq", str(self.spn_exp_subq.value()), "-c:a", "copy"])
            if self.txt_exp_custom.toPlainText().strip():
                args.extend(self.txt_exp_custom.toPlainText().strip().split())

        return args, extension, loops, tag

    def select_video_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Video auswählen", "", "Video Files (*.mp4 *.mkv *.mov *.avi)")
        if file_path:
            norm_path = to_long_path(file_path)
            self.txt_file_path.setText(norm_path)
            if not self.txt_target_path.text():
                self.txt_target_path.setText(os.path.dirname(norm_path))

    def select_target_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Zielordner auswählen")
        if dir_path:
            self.txt_target_path.setText(to_long_path(dir_path))

    def select_ffmpeg_exe(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "FFmpeg Executable auswählen", "", "Executable (*.exe)")
        if file_path:
            self.txt_ffmpeg_path.setText(to_long_path(file_path))
            self.update_hardware_options()

    def select_temp_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Temp-Ordner / RAM-Disk auswählen")
        if dir_path:
            self.txt_temp_path.setText(to_long_path(dir_path))

    def start_process(self):
        input_path = to_long_path(self.txt_file_path.text().strip())
        target_dir = to_long_path(self.txt_target_path.text().strip())
        ffmpeg_exe = to_long_path(self.txt_ffmpeg_path.text().strip())
        temp_dir = to_long_path(self.txt_temp_path.text().strip())

        if not input_path or not os.path.exists(input_path):
            QMessageBox.critical(self, "Fehler", "Bitte wähle eine gültige Eingangsdatei aus!")
            return

        if not target_dir:
            QMessageBox.critical(self, "Fehler", "Bitte wähle einen Zielordner aus!")
            return

        if not shutil.which(ffmpeg_exe) and not os.path.exists(ffmpeg_exe):
            QMessageBox.critical(self, "Fehler", "FFmpeg wurde nicht gefunden! Bitte Pfad überprüfen.")
            return

        if not os.path.exists(temp_dir):
            QMessageBox.critical(self, "Fehler", "Der gewählte Temp-Ordner existiert nicht!")
            return

        args, extension, total_loops, custom_tag = self.get_active_config()
        keep_ms = self.chk_keep_milestones.isChecked()
        ms_step = self.spn_milestone_step.value()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        self.worker = FFmpegWorker(
            ffmpeg_exe, input_path, target_dir, temp_dir, args, extension, total_loops,
            custom_tag=custom_tag, keep_milestones=keep_ms, milestone_interval=ms_step
        )
        self.worker.progress_current.connect(self.pbar_current.setValue)
        self.worker.progress_overall.connect(lambda cur, tot: self.pbar_overall.setValue(int((cur / tot) * 100)))
        self.worker.log_signal.connect(print)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def stop_process(self):
        if self.worker:
            self.worker.stop_gracefully()
            self.btn_stop.setText("Stoppe nach Durchlauf...")
            self.btn_stop.setEnabled(False)

    def on_finished(self, msg):
        QMessageBox.information(self, "Ergebnis / Status", msg)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("Nach aktuellem Durchlauf stoppen")
        self.pbar_current.setValue(100)
        self.pbar_overall.setValue(100)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoLabApp()
    window.show()
    sys.exit(app.exec())