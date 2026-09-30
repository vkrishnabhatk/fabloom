#!/bin/sh
# Bake the JVM AOT cache (JEP 514, JDK 25; the successor to an AppCDS archive) at image
# build time. It is a *file*, so unlike a warm JVM it survives Sandboxes checkpoints: every
# L3 candidate execution starts Batfish from it (ADR-0003).
#
# Training run: start Batfish with -XX:AOTCacheOutput, drive it through the exact questions
# fabloom asks (the committed training jobs), then stop it cleanly so the cache is written.
set -eu
cd /
java -XX:AOTCacheOutput=/opt/fabloom/batfish.aot \
  -XX:-UseCompressedOops -XX:+UseContainerSupport -XX:MaxRAMPercentage=80 \
  -Dlog4j.configurationFile=log4j2.yaml \
  -cp allinone-bundle.jar org.batfish.allinone.Main \
  -runclient false -loglevel warn \
  -coordinatorargs "-templatedirs questions -containerslocation /data/containers" &
PID=$!
for job in /opt/fabloom/training/*.json; do
  fabloom-verify --job "$job" --startup-timeout 300 > /dev/null
done
kill -TERM "$PID"
wait "$PID" || true
test -s /opt/fabloom/batfish.aot
echo "AOT cache: $(du -h /opt/fabloom/batfish.aot | cut -f1)"
