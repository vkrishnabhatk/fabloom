#!/bin/sh
# Start the Batfish server the way the base image's wrapper.sh does (read from
# batfish/allinone:2026.08.27.3685), minus Jupyter, plus the baked AOT cache when present.
# Nebius Sandboxes ignore the image start command, so fabloom-verify runs this itself via
# FABLOOM_BATFISH_START_CMD (ADR-0003).
set -eu
cd /
AOT=""
if [ -s /opt/fabloom/batfish.aot ]; then
  AOT="-XX:AOTCache=/opt/fabloom/batfish.aot"
fi
# shellcheck disable=SC2086
exec java $AOT \
  -XX:-UseCompressedOops -XX:+UseContainerSupport \
  -XX:MaxRAMPercentage="${FABLOOM_BATFISH_RAM_PCT:-80}" \
  -Dlog4j.configurationFile=log4j2.yaml \
  -cp allinone-bundle.jar org.batfish.allinone.Main \
  -runclient false -loglevel warn \
  -coordinatorargs "-templatedirs questions -containerslocation /data/containers"
