#!/bin/sh
set -eu

cd /app

python -m scripts.clientplatform_production_preflight
python -m scripts.clientplatform_monetization_preflight
python -m scripts.clientplatform_program_media_preflight
python -m scripts.clientplatform_bot_gateway_preflight

exec python main.py
