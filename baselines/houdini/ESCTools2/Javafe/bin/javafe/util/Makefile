# $Id$
# Copyright 2000, 2001, Compaq Computer Corporation; 2006 dsrg.org.

runtest:
	@echo "TESTING Javafe: java/javafe/util (note some tests have been disabled -- see Makefile)"
	@${JAVA} -classpath ${CLASSPATH} javafe.util.CorrelatedReaderTest subreader < CorrelatedReader.java
	@${JAVA} -classpath ${CLASSPATH} javafe.util.CorrelatedReaderTest reset < CorrelatedReader.java

diabled_tests:
	# below tests removed because they exceeds CorrelatedReaderTest max
	# file size:
	#${JAVA} javafe.util.CorrelatedReaderTest subreader <../parser/test/compare-exprs-big.j
	#${JAVA} javafe.util.CorrelatedReaderTest reset <../parser/test/compare-exprs-big.j
	#
	# This test is disabled  because it is only useful with a human
	# observing the output:
	#${JAVA} javafe.util.CorrelatedReaderTest locs <CorrelatedReader.java
