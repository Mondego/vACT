#!/bin/bash
# Exit codes:
# 1  - wrong arguments
# 2  - compile error
# 3  - DynComp timeout
# 4  - Chicory (trace generation) failure
# 5  - Daikon inference failure
# 6  - JML annotation failure
# 7  - ESC annotation failure

cleanup() {
  rm -f *.txt *.class *.dtrace.gz *.decls-DynComp
}

if [ $# -ne 3 ]; then
  echo "Usage: $0 <class_name> <test_class_name> <number_of_test>"
  exit 1  # exit code 1: wrong arguments
fi


class_name=$1 
test_class_name=$2
numberOfTest=$3

# Daikon's DynComp runtime (java/dcomp_rt.jar) was built with JDK 11, so the whole pipeline runs on JDK 11.
# DynComp and Chicory are run as Java agents in the target JVM itself: their launchers start a second JVM and,
# with stdin redirected from a file, never return (the target program finishes, the launcher hangs).
JAVA=${DAIKON_JAVA:-/pkg/openjdk/11.0.2/bin/java}
JAVAC=${DAIKON_JAVAC:-/pkg/openjdk/11.0.2/bin/javac}
D=$DAIKONDIR

echo "Class Name: $class_name"
echo "Test Class Name: $test_class_name"
echo "Number of Test: $numberOfTest"

echo $numberOfTest

echo "Step-1: Compile the program with the -g switch to enable debugging symbols."
$JAVAC -g *.java
if [ $? -ne 0 ]; then
    echo "Step-1: Compile failed"
    cleanup
    exit 2  # exit code 2: compile error
fi
echo "Step-1: Done"


echo "Step-3: Run the program under the control of the Chicory front"

echo "Step-3a: Generate comparability information and trace file for test: 0"
# [FIXED TIME 5 min to generate decls-DynComp once]
timeout 300 $JAVA -cp .:$D/daikon.jar -ea -esa -Xmx2g --add-opens java.base/java.lang=ALL-UNNAMED \
  -Xbootclasspath/a:$D/java/dcomp_rt.jar:$D/daikon.jar --add-reads java.base=ALL-UNNAMED \
  --add-exports java.base/sun.util.locale=ALL-UNNAMED --patch-module java.base=$D/java/dcomp_rt.jar \
  "-javaagent:$D/java/dcomp_premain.jar=--decl-file=Test_0.decls-DynComp" $test_class_name < 0.txt
if [ $? -eq 124 ]; then
    echo "Step-3-a: Exit DynComp timed out"
    cleanup
    exit 3  # exit code 3: DynComp timeout
fi
echo "Step-3-a: Done"

echo "Step-3-b: Generating trace file for all tests"
counter=0
while [ $counter -lt $numberOfTest ]; do 
  
  timeout 120 $JAVA -cp .:$D/daikon.jar \
    "-javaagent:$D/java/ChicoryPremain.jar=--dtrace-file=Test_$counter.dtrace.gz --comparability-file=Test_0.decls-DynComp" \
    $test_class_name < $counter.txt
  if [ $? -ne 0 ]; then
      echo "Step-3-b: Chicory failed for test $counter"
      cleanup
      exit 4  # exit code 4: trace generation failure
  fi
  ((counter++))
done
echo "Step-3-b: Done"


echo "Step-3-c: Run Daikon on the trace files."
#[With] Customized Config
#java -cp .:$DAIKONDIR/daikon.jar daikon.Daikon --config inv-config.config --suppress_redundant Test*.dtrace.gz
#[With] Default Config
$JAVA -cp .:$D/daikon.jar daikon.Daikon --suppress_redundant Test*.dtrace.gz
if [ $? -ne 0 ]; then
    echo "Step-3-c: Daikon inference failed"
    cleanup
    exit 5  # exit code 5: Daikon inference failure
fi
echo "Step-3-c: Done"


echo "Step-4: Annotate the source with JML"
$JAVA -cp .:$D/daikon.jar daikon.tools.jtb.Annotate --no_reflection --format jml Test*.inv.gz $class_name.java
if [ $? -ne 0 ]; then
    echo "Step-4: JML annotation failed"
    cleanup
    exit 6  # exit code 6: JML annotation failure
fi
echo "Step-4: Done"

echo "Step-5: Annotate the source with ESC/Java2 with the same invariants."
$JAVA -cp .:$D/daikon.jar daikon.tools.jtb.Annotate --no_reflection --format esc Test*.inv.gz $class_name.java
if [ $? -ne 0 ]; then
    echo "Step-5: ESC annotation failed"
    cleanup
    exit 7  # exit code 7: ESC annotation failure
fi
echo "Step-5: Done"

echo "Step-6: Clean up input (*.txt), compiled (*.class) and trace (*.dtrace.gz) files."
cleanup
echo "Step-6: Done"