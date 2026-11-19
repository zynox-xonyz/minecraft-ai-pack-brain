#!/usr/bin/env bash
# Pack-Brain: Installation und Update im Minecraft-LXC (als root, z. B. in der Proxmox-Konsole).
#   bash install.sh <PC-IP>:<Port>
# Erstinstallation und Update sind derselbe Aufruf. Bei einem Update bleiben config.json,
# db/cards.sqlite, db/notebook.sqlite und logs/ auf dem Server unveraendert.
set -euo pipefail

SRC="${1:?Aufruf: bash install.sh <PC-IP>:<Port>}"
case "$SRC" in *:*) ;; *) SRC="$SRC:8000" ;; esac
APP=/opt/packbrain

[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausfuehren."; exit 1; }

echo "== 1/7 Pakete installieren (python3, venv, curl)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv curl >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 oder neuer noetig, gefunden: $(python3 --version)"; exit 1; }

echo "== 2/7 Paket vom PC laden (http://$SRC)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsS -o "$TMP/p.tgz" "http://$SRC/packbrain-upload.tar.gz"
mkdir "$TMP/x"
tar -xzf "$TMP/p.tgz" -C "$TMP/x"

echo "== 3/7 Dateien nach $APP kopieren"
id packbrain >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin packbrain
systemctl stop packbrain 2>/dev/null || true
mkdir -p "$APP/db" "$APP/logs" "$APP/extract"
rm -rf "$APP/packbrain"
cp -r "$TMP/x/packbrain" "$APP/"
cp "$TMP/x/requirements.txt" "$APP/"
cp "$TMP/x/extract/make_cards.py" "$APP/extract/"
cp "$TMP/x/db/packbrain.sqlite" "$APP/db/packbrain.sqlite.new"
mv -f "$APP/db/packbrain.sqlite.new" "$APP/db/packbrain.sqlite"
if [ -f "$APP/db/cards.sqlite" ]; then echo "   cards.sqlite existiert schon - bleibt"; else cp "$TMP/x/db/cards.sqlite" "$APP/db/"; fi
if [ -f "$APP/config.json" ];      then echo "   config.json existiert schon - bleibt";  else cp "$TMP/x/config.json" "$APP/"; fi

echo "== 4/7 config.json: nur lokal lauschen (127.0.0.1)"
python3 - "$APP/config.json" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p, encoding="utf-8"))
c["host"] = "127.0.0.1"
json.dump(c, open(p, "w", encoding="utf-8"), indent=2)
print("   host=%s port=%s agent=%s llm=%s" % (c["host"], c.get("port", 8787), c.get("agent", "v3"), c.get("llm_base_url")))
PY
LLM=$(python3 -c 'import json; print(json.load(open("/opt/packbrain/config.json")).get("llm_base_url", "http://127.0.0.1:8080/v1"))')
if curl -fsS -m 10 "$LLM/models" >/dev/null; then echo "   llama-swap erreichbar"; else echo "   WARNUNG: llama-swap unter $LLM nicht erreichbar - Antworten werden fehlschlagen"; fi

echo "== 5/7 Python-Umgebung (dauert beim ersten Mal 1-2 Minuten)"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"
"$APP/.venv/bin/python" -m compileall -q "$APP/packbrain" "$APP/extract"
chown -R packbrain:packbrain "$APP"
chmod 600 "$APP/config.json"

echo "== 6/7 systemd-Dienst"
cat > /etc/systemd/system/packbrain.service <<'UNIT'
[Unit]
Description=Pack-Brain backend (in-game assistant)
After=network-online.target
Wants=network-online.target

[Service]
User=packbrain
WorkingDirectory=/opt/packbrain
ExecStart=/opt/packbrain/.venv/bin/python -m packbrain
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/packbrain/logs /opt/packbrain/db

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now packbrain >/dev/null 2>&1
systemctl restart packbrain

echo "== 7/7 Test"
ok=0
for _ in $(seq 1 60); do
  if curl -fsS -m 2 http://127.0.0.1:8787/v1/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  echo "FEHLER: Dienst antwortet nicht. Log:"
  journalctl -u packbrain -n 30 --no-pager
  exit 1
fi
echo "   /v1/health OK"
TOKEN=$(python3 -c 'import json; print(json.load(open("/opt/packbrain/config.json"))["api_token"])')
echo "   Testfrage 'wie crafte ich eine truhe' (bis zu 2 Minuten) ..."
if curl -sN -m 120 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"player":"install-test","question":"wie crafte ich eine truhe"}' \
     http://127.0.0.1:8787/v1/ask | grep -q '^event: done'; then
  echo "   Testfrage beantwortet"
else
  echo "   WARNUNG: Testfrage ohne Antwort - 'journalctl -u packbrain -n 50' pruefen"
fi

echo ""
echo "FERTIG. Pack-Brain laeuft als Dienst 'packbrain' auf 127.0.0.1:8787."
echo "Naechster Schritt: in config/packbrain-common.toml des Minecraft-Servers"
echo "    backendUrl = \"http://127.0.0.1:8787\""
echo "setzen (apiToken bleibt gleich) und den Minecraft-Server neu starten."
