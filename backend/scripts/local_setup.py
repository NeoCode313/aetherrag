"""Open first-owner registration locally without printing installation credentials."""
from pathlib import Path
import argparse
import os
import sys
import webbrowser
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.infrastructure.config import get_settings
from app.infrastructure.installation_state import InstallationState

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--reset-password", action="store_true")
    args=parser.parse_args()
    if args.url not in {"http://localhost:8000","http://127.0.0.1:8000"}:
        parser.error("El registro inicial nativo solo se abre en loopback")
    settings=get_settings()
    store=InstallationState(settings.data_dir,settings.operator_username,settings.operator_password_hash,settings.session_secret)
    state=store.read()
    if args.reset_password:
        from getpass import getpass
        from argon2 import PasswordHasher
        password=getpass("Nueva contraseña propia (mínimo 15 caracteres): ")
        if not 15 <= len(password) <= 512 or password != getpass("Repetir contraseña: "):
            parser.error("Contraseña inválida o confirmación diferente")
        store.rotate_password(PasswordHasher().hash(password))
        print("Contraseña actualizada. Las sesiones anteriores fueron invalidadas.")
        return
    if state.get("owner"):
        print("El propietario ya está creado. Abriendo inicio de sesión.")
        webbrowser.open(args.url)
    else:
        # A URL fragment is not sent to the HTTP server; the client removes it immediately.
        webbrowser.open(args.url+"/#setup="+quote(state["setup_token"],safe=""))
        print("Se abrió el registro inicial en el navegador. El código no se imprimió ni se envió en la URL HTTP.")

if __name__=="__main__":
    main()
