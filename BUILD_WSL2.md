# Buildozer en Windows con WSL2

En PowerShell administrador:

```powershell
wsl --install -d Ubuntu
```

Dentro de Ubuntu:

```bash
sudo apt update
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip \
python3-virtualenv autoconf libtool pkg-config zlib1g-dev \
libncurses5-dev libncursesw5-dev libtinfo6 cmake libffi-dev \
libssl-dev automake autopoint gettext curl

curl https://sh.rustup.rs -sSf | sh
source "$HOME/.cargo/env"

python3 -m venv ~/buildozer-venv
source ~/buildozer-venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install git+https://github.com/kivy/buildozer legacy-cgi cython==0.29.34
```

Clona el proyecto dentro del filesystem Linux, no en `/mnt/c/...`:

```bash
cd ~
git clone https://github.com/TU_USUARIO/TU_REPO.git spamguard-es
cd spamguard-es/android_app
```

Si compilas localmente, modifica `repo_config.json` con tu URL RAW:

```json
{
  "raw_base": "https://raw.githubusercontent.com/TU_USUARIO/TU_REPO/main/data"
}
```

Compila:

```bash
buildozer -v android debug
```

El APK queda en `android_app/bin/`.
