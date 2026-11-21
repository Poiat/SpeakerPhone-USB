#!/usr/bin/env python3
"""
Registro de logs en disco multiplataforma para SpeakerPhone-USB.

Escribe un archivo rotativo (2 MB por archivo, 5 copias) en la carpeta de
logs propia de cada sistema operativo:

- Linux:   $XDG_STATE_HOME/SpeakerPhone-USB/logs
           (por defecto ~/.local/state/SpeakerPhone-USB/logs)
- Windows: %LOCALAPPDATA%\\SpeakerPhone-USB\\logs
- macOS:   ~/Library/Logs/SpeakerPhone-USB

Se puede forzar otra carpeta con la variable de entorno
SPEAKERPHONE_LOG_DIR. Si la ubicación preferida no es escribible, se usa
una carpeta temporal como respaldo.

Uso básico:
    from app_logger import setup_logging, log, install_exception_hooks

    setup_logging()
    install_exception_hooks()
    log("Aplicación iniciada")
"""

import logging
import logging.handlers
import os
import platform
import subprocess
import sys
import tempfile
import threading

APP_NAME = "SpeakerPhone-USB"
LOGGER_NAME = "speakerphone"
LOG_FILENAME = "speakerphone.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_lock = threading.Lock()
_hooks_lock = threading.Lock()
_logger = None
_log_path = None
_log_dir = None
_hooks_installed = False


def _log_dir_for(system):
    """Devuelve la carpeta de logs recomendada para el sistema indicado."""
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, APP_NAME, "logs")
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~/Library/Logs"), APP_NAME)
    # Linux y otros UNIX: seguir la especificación XDG.
    state_home = os.environ.get("XDG_STATE_HOME")
    if not state_home or not os.path.isabs(state_home):
        state_home = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state_home, APP_NAME, "logs")


def get_log_dir():
    """Carpeta donde se guardan los logs (sin crearla todavía)."""
    override = os.environ.get("SPEAKERPHONE_LOG_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    try:
        return _log_dir_for(platform.system())
    except Exception:
        return os.path.join(tempfile.gettempdir(), f"{APP_NAME}-logs")


def setup_logging(level=logging.INFO, console=False, log_dir=None):
    """
    Configura el logger de la aplicación y devuelve una instancia de
    logging.Logger. Es idempotente: las llamadas posteriores reutilizan la
    configuración existente.

    :param level: nivel mínimo de registro (por defecto logging.INFO).
    :param console: si es True, además escribe en stderr.
    :param log_dir: carpeta de logs explícita (útil para pruebas).
    """
    global _logger, _log_path, _log_dir
    with _lock:
        if _logger is not None:
            return _logger

        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(level)
        logger.propagate = False

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        candidates = []
        if log_dir:
            candidates.append(os.path.abspath(os.path.expanduser(log_dir)))
        candidates.append(get_log_dir())
        candidates.append(os.path.join(tempfile.gettempdir(), f"{APP_NAME}-logs"))

        seen = set()
        for directory in candidates:
            if directory in seen:
                continue
            seen.add(directory)
            try:
                os.makedirs(directory, exist_ok=True)
                path = os.path.join(directory, LOG_FILENAME)
                handler = logging.handlers.RotatingFileHandler(
                    path,
                    maxBytes=MAX_BYTES,
                    backupCount=BACKUP_COUNT,
                    encoding="utf-8",
                )
                handler.setFormatter(formatter)
                logger.addHandler(handler)
                _log_path = path
                _log_dir = directory
                break
            except OSError:
                continue

        if _log_path is None:
            # Nunca romper la app por no poder escribir logs.
            logger.addHandler(logging.NullHandler())

        if console:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)

        _logger = logger
        return logger


def get_logger():
    """Devuelve el logger configurado (lo configura si aún no existe)."""
    return setup_logging()


def get_log_path():
    """Ruta completa del archivo de log activo, o None si no está disponible."""
    setup_logging()
    return _log_path


def get_log_dir_path():
    """Carpeta de logs activa, o None si no está disponible."""
    setup_logging()
    return _log_dir


def log(message, level=logging.INFO):
    """Escribe un mensaje en el log de disco."""
    setup_logging().log(level, message)


def log_exception(exc_type, exc_value, exc_tb, context="Excepción no controlada"):
    """Registra una excepción con su traza completa."""
    setup_logging().error(
        "%s: %s: %s",
        context,
        getattr(exc_type, "__name__", exc_type),
        exc_value,
        exc_info=(exc_type, exc_value, exc_tb),
    )


def log_startup_info():
    """Registra información útil del entorno al iniciar la aplicación."""
    logger = setup_logging()
    logger.info("=" * 64)
    logger.info("Iniciando %s", APP_NAME)
    logger.info(
        "Sistema: %s %s (%s)",
        platform.system(),
        platform.release(),
        platform.machine(),
    )
    logger.info("Python: %s", sys.version.split()[0])
    logger.info("Archivo de log: %s", _log_path or "no disponible")


def install_exception_hooks(logger=None):
    """
    Instala hooks globales para que las excepciones no controladas del hilo
    principal y de cualquier hilo se guarden en disco antes de mostrarse por
    la salida de error habitual.
    """
    global _hooks_installed
    with _hooks_lock:
        if _hooks_installed:
            return
        _hooks_installed = True

    logger = logger or setup_logging()
    previous_hook = sys.excepthook

    def handle_exception(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.error(
                "Excepción no controlada en el hilo principal",
                exc_info=(exc_type, exc_value, exc_tb),
            )
        try:
            previous_hook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = handle_exception

    if hasattr(threading, "excepthook"):
        default_thread_hook = threading.excepthook

        def handle_thread_exception(args):
            if not (args.exc_type and issubclass(args.exc_type, SystemExit)):
                thread_name = args.thread.name if args.thread else "desconocido"
                logger.error(
                    "Excepción no controlada en el hilo '%s'",
                    thread_name,
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            try:
                default_thread_hook(args)
            except Exception:
                pass

        threading.excepthook = handle_thread_exception


def open_log_folder():
    """
    Abre la carpeta de logs en el explorador de archivos del sistema.
    Devuelve True si se pudo lanzar el comando.
    """
    directory = get_log_dir_path()
    if not directory:
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(directory)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", directory])
        else:
            # En AppImage, LD_LIBRARY_PATH apunta a las librerías empaquetadas
            # y puede impedir que xdg-open arranque herramientas del sistema.
            env = os.environ.copy()
            original = env.pop("APPIMAGE_ORIG_LD_LIBRARY_PATH", None)
            if original:
                env["LD_LIBRARY_PATH"] = original
            elif "APPIMAGE" in env:
                env.pop("LD_LIBRARY_PATH", None)
            subprocess.Popen(["xdg-open", directory], env=env)
        return True
    except Exception:
        return False
