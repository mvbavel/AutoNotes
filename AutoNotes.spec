# -*- mode: python ; coding: utf-8 -*-
# AutoNotes.spec — PyInstaller build spec
# Build with:  ~/Library/Python/3.9/bin/pyinstaller AutoNotes.spec

block_cipher = None

from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files

# ── Collect namespace packages (pyannote.*) ───────────────────────────────────
_pyannote_pkgs = [
    'pyannote', 'pyannote.audio', 'pyannote.core',
    'pyannote.database', 'pyannote.metrics', 'pyannote.pipeline',
]
_pa_datas, _pa_binaries, _pa_hidden = [], [], []
for _pkg in _pyannote_pkgs:
    try:
        d, b, h = collect_all(_pkg)
        _pa_datas += d; _pa_binaries += b; _pa_hidden += h
    except Exception:
        pass

# ── Collect faster-whisper / CTranslate2 ─────────────────────────────────────
_ct2_datas, _ct2_binaries, _ct2_hidden = collect_all('ctranslate2')
_fw_datas,  _fw_binaries,  _fw_hidden  = collect_all('faster_whisper')

# ── OpenCV ────────────────────────────────────────────────────────────────────
_cv2_datas, _cv2_binaries, _cv2_hidden = collect_all('cv2')

# ── Anthropic SDK + HTTP stack ────────────────────────────────────────────────
_anth_datas, _anth_binaries, _anth_hidden = collect_all('anthropic')
_httpx_datas, _httpx_binaries, _httpx_hidden = collect_all('httpx')
_httpcore_datas, _httpcore_binaries, _httpcore_hidden = collect_all('httpcore')

# ── yt-dlp binary (invoked as subprocess; no Python module needed) ────────────
_ytdlp_datas, _ytdlp_binaries, _ytdlp_hidden = [], [], []

# ── Torch submodules (hooks-contrib handles core; we add explicit hidden) ─────
_torch_hidden = collect_submodules('torch')
_torchaudio_hidden = collect_submodules('torchaudio')
_torch_datas = collect_data_files('torch')
_torchaudio_datas = collect_data_files('torchaudio')
_lightning_hidden = collect_submodules('pytorch_lightning')
_lightning_datas = collect_data_files('pytorch_lightning')

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=(
        [
            ('/opt/homebrew/bin/ffmpeg',  '.'),
            ('/opt/homebrew/bin/ffprobe', '.'),
            ('/opt/homebrew/bin/yt-dlp',  '.'),
        ]
        + _ct2_binaries + _fw_binaries + _cv2_binaries
        + _pa_binaries + _anth_binaries + _httpx_binaries
        + _httpcore_binaries + _ytdlp_binaries
    ),
    datas=(
        _ct2_datas + _fw_datas + _cv2_datas
        + _pa_datas + _anth_datas + _httpx_datas
        + _httpcore_datas + _ytdlp_datas
        + _torch_datas + _torchaudio_datas + _lightning_datas
    ),
    hiddenimports=(
        _ct2_hidden + _fw_hidden + _cv2_hidden
        + _pa_hidden + _anth_hidden + _httpx_hidden
        + _httpcore_hidden + _ytdlp_hidden
        + _torch_hidden + _torchaudio_hidden + _lightning_hidden
        + [
            # PyQt6
            'PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui',
            'PyQt6.QtNetwork', 'PyQt6.QtPrintSupport', 'PyQt6.sip',
            # Numeric / audio
            'numpy', 'scipy', 'scipy.signal', 'scipy.io', 'scipy.io.wavfile',
            'soundfile', 'sounddevice', 'librosa', 'numba', 'llvmlite',
            'audioread', 'resampy', 'samplerate',
            # ML support
            'einops', 'asteroid_filterbanks', 'speechbrain',
            'torchmetrics', 'torch_audiomentations',
            # Misc
            'pkg_resources', 'packaging', 'certifi',
            'charset_normalizer', 'idna', 'docx', 'PIL',
        ]
    ),
    hookspath=[],
    hooksconfig={
        'matplotlib': {'backends': 'none'},
    },
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'notebook', 'ipython', 'IPython', 'jupyter',
        'tkinter', '_tkinter', 'wx', 'gi',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AutoNotes',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can corrupt arm64 Mach-O binaries
    console=False,      # macOS windowed app — no terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,   # inherit host arch (arm64)
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AutoNotes',
)

app = BUNDLE(
    coll,
    name='AutoNotes.app',
    icon=None,
    bundle_identifier='com.autonotes.app',
    version='1.0.0',
    info_plist={
        'CFBundleName': 'AutoNotes',
        'CFBundleDisplayName': 'AutoNotes',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1',
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
        'NSAppleScriptEnabled': False,
        'NSRequiresAquaSystemAppearance': False,  # support dark mode
        'NSHumanReadableCopyright': '© 2025 AutoNotes',
    },
)
