# $Id$

export TOP = ..
include ${TOP}/Makefile.defs

###########################################################################
# build targets

SOURCEFILES := junitutils/*.java
CLASSESSTAMP = classes.stamp
CLASSDIRECTORY=.

CP = ${call canonicalize,${UTILS_CLASSFILES}:${JUNIT_LIB}:$(SPECS)}

export PROVER_KILL_TIME ?= 7

#------------------------------------------------------------------------------

.PHONY: clean test build esctest compile buildall compileall

build:    checkjunitjar compile
buildall: checkjunitjar compileall

compile: ${CLASSESSTAMP}
compileall: clean compile

${CLASSESSTAMP}: ${SOURCEFILES}
ifdef USE_JMLC
	$(JMLC) $(JMLC_FLAGS) $?
	@# For the command on the previous line to succeed, you need to 
	@# have junit and the JML run-time jar & specs in your class path.
	@#$(JMLC) $(JMLC_FLAGS) --classpath $(CP) $?
else
	${JAVAC} -classpath ${CP} $?
endif
	touch ${CLASSESSTAMP}

.PHONY: checkjunitjar
checkjunitjar:
	@if [ ! -e ${JUNIT_LIB} ]; then \
		echo Error: ${JUNIT_LIB} should be a version of junit.jar, but it does not exist ;\
		exit 1; \
	fi

test: build

self_test: build
	@echo CLASSPATH used: ${CP}
	@echo PROVER_KILL_TIME=$(PROVER_KILL_TIME)
	${ESCJ} -classpath ${CP} \
		-specs ${SPECS} \
		${ESCJ_EXTRA_FLAGS} \
		${SOURCEFILES}

self_test0:
	export SPECS="$(HOME)/dev/JMLspecs/java4"; \
	export PROVER_KILL_TIME=5; \
	$(MAKE) ESCJ_EXTRA_FLAGS+=" -nonNullByDefault" \
		SPECS="$(HOME)/dev/JMLspecs/java4" \
		ESCJ_VERBOSE=1 self_test

clean:
	@-rm -f junitutils/*.class ${CLASSESSTAMP}

.PHONY: docs
docs:
	@true

.PHONY: info
info:
	@echo "This package contains: " `cat $(SOURCEFILES) | wc -l` " LOC."

# End of file
