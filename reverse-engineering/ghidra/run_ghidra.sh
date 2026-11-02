#!/bin/sh
export GHIDRA_MAXMEM=8G
export JAVA_HOME=/opt/homebrew/opt/openjdk@25/libexec/openjdk.jdk/Contents/Home
export PATH=$JAVA_HOME/bin:$PATH
"/opt/homebrew/Cellar/ghidra/12.2/libexec/support/analyzeHeadless" $PWD/work/ghidra_proj pclink -import "$PWD/work/extracted/app/PCLink G5/PCLink.exe" -scriptPath $PWD/work/ghidra_scripts -postScript ExportAll.java $PWD/work/PCLink.c -max-cpu 8 -analysisTimeoutPerFile 3000 > $PWD/work/ghidra.log 2>&1
rc=$?; echo "ghidra exit $rc"; exit $rc
