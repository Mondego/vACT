#!/usr/local/bin/perl
# Copyright (c) 1999, Compaq Computer Corporation
# Change history:
#   16 Sep 1999  rustan & flanagan  Created
#   27 Sep 1999  rustan & flanagan  Added support to insert warnings into source files
#    7 Dec 1999  rustan & flanagan  Added file-less Cautions/Warnings/etc.
#   16 Sep 2000  flanagan           Sorted warnings into clusters based on suggestions
#   18 Sep 2000  flanagan           Tabularized warning summary
#   21 Sep 2000  flanagan           Put clusters at start of html
#   15 Nov 2000  flanagan & freund  Moved to Javafe package from Escjava
#   15 Dec 2000  flanagan & freund  Adapted to cluster rcc warnings
# USAGE: warnings2html [-d <directory of htmlized sources>] [-p <package name>]
#                      < tool-output
#                      > warnings.html
sub htmlize {
  my $str = $_[0];
  my $strpre = "";
  while ($str =~ /^(.*)&(.*)$/) {
      $strpre .= $1 . "&amp;";
      $str = $2;
  }
  $str = $strpre . $str;
  while ($str =~ /^(.*)<(.*)$/) {
      $str = $1 . "&lt;" . $2;
  }
  while ($str =~ /^(.*)>(.*)$/) {
      $str = $1 . "&gt;" . $2;
  }
  return $str;
}

# Remove the leading "./", if any, and then change any remaining
# occurrences of "/" into ".".
sub filenameFlatten {
    my $name = $_[0];
    if ($name =~ /^\.\/(.*)$/) {
	$name = $1;
    }
    while ($name =~ /^(.*)\/(.*)$/) {
	$name = $1 . "." . $2;
    }
    return $name;
}

sub linkifyLoc {
    if ($_[0] =~ /^(.*) (\d*) (\d*)$/) {
	return "<A HREF=\"" . filenameFlatten($1) . ".html#" .
	    "$2\">" . htmlize($1 . ", line " . $2 . ", col " . $3) . 
		"</A>";
    } else {
	die "Bad arg to linkifyLoc: $_[0]";
    }
}

sub makeHrefLoc {
    if ($_[0] =~ /^(.*) (\d*) (\d*)$/) {
	return "<A HREF=\"" . filenameFlatten($1) . ".html#" .
	    "$2\">";
    } else {
	die "Bad arg to makeHrefLoc: $_[0]";
    }
}



$srcHtmlDir = "";
if ( $ARGV[0] eq "-d" ) {
    $srcHtmlDir = $ARGV[1];
}

$packagePrefix = "";
if ( $ARGV[0] eq "-p" ) {
    $packagePrefix = $ARGV[1];
} elsif ( $ARGV[2] eq "-p" ) {
    $packagePrefix = $ARGV[3];
}

#############################################################
# First try to parse 
#   (1) ../log/coordinator.out, which shows where each annotation is refuted
#   (2) ../log/annlinkfile.txt, which shows where annotations are used
#############################################################

%coord = ();
%declAnnLinks = ();
%warnDeclLinks = ();
%declNames = ();
%annText = ();

if (open(COORD, "../log/coordinator.out") &&
    open(ANNLINKS, "../log/annlinkfile.txt")) {

    # parse them
    while (!eof(ANNLINKS)) {
	$_ = <ANNLINKS>;
	chop;
	# example input are
	#   DeclAnn ./C.java 2 87 ./C.java 2 75
	#   WarnDecl ./C.java 18 3 ./C.java 4 87
	#   DeclName ./C.java 2 87 field x
	#   AnnText ./C.java 2 32 /*@ spec_public */

	if ($_ =~ /^DeclAnn (.*) (\d*) (\d*) (.*) (\d*) (\d*)$/ ) {
	    $declloc = "$1 $2 $3";
	    $annloc = "$4 $5 $6";
	    $declAnnLinks{$declloc} = $annloc;
	} elsif ($_ =~ /^WarnDecl (.*) (\d*) (\d*) (.*) (\d*) (\d*)$/ ) {
	    $warnloc = "$1 $2 $3";
	    $declloc = "$4 $5 $6";
	    $warnDeclLinks{$warnloc} = $declloc;
	} elsif ($_ =~ /^DeclName (.*) (\d*) (\d*) (.*)$/ ) {
	    $declloc = "$1 $2 $3";
	    $declNames{$declloc} = $4;
	} elsif ($_ =~ /^AnnText (.*) (\d*) (\d*) (.*)$/ ) {
	    $annloc = "$1 $2 $3";
	    $annText{$annloc} = $4;
	} else {
	    die "Bad line in annlinksfile.txt: $_";
	}
    }

    $_ = <COORD>;
    while (!eof(COORD)) {
	chop;
	if ($_ =~ /^([^:]*):(\d*): Warning: (.*)$/) {
	    $sourceFilename = $1;
	    $sourceLineNumber = $2;
	    $message = $3;

	    $_ = <COORD>;
#    if ($_ =~ /^(Associated declaration is )\"([^\"]*)\", line (\d*), col (\d*):$/) {
#	    } else {
#		$_ = <COORD>;
#		$_ = <COORD>;
#		chop;
#	    }
#	    #print STDERR $_;
#	    #print STDERR "\n";
 	    if ($_ =~ /^(Associated declaration is )\"([^\"]*)\", line (\d*), col (\d*):$/) {
		$locann = "$2 $3 $4";

		$_ = <COORD>;
#    	if ($_ =~ /^Suggestion \[(\d*),(\d*)\]: (.*)$/) {
#		} else {
#		    $_ = <COORD>;
#		    $_ = <COORD>;
#	 	    chop;
#		}	    
 		if ($_ =~ /^Suggestion \[(\d*),(\d*)\]: (.*)$/) {
		    $locwarn = "$sourceFilename $sourceLineNumber $2";
		    $coord{$locann} = $locwarn . "::" . $message;
		}
	    }
	}
	$_ = <COORD>;
    }
}


#    print STDERR %coord;
#    print STDERR "\n";
#    print STDERR %declAnnLinks;
#    print STDERR "\n";
#    print STDERR %warnDeclLinks;
#    print STDERR "\n";
#    print STDERR %declNames;
#    print STDERR "\n";
#    print STDERR %annText;
#    print STDERR "\n";


#################################################################

$currentClass = "";
$currentRoutine = "";
$records = [];

$_ = <STDIN>;
while (!eof(STDIN)) {
    chop;
    if ($_ =~ /^(.*): (.*\)) ...$/) {
	$currentClass = $1;
	$currentRoutine = $2;
	$_ = <STDIN>;
	next;
    } elsif ($_ =~ /^([^:]*):(\d*): (Warning: .*)$/ ||
	     $_ =~ /^([^:]*):(\d*): (Caution: .*)$/ ||
	     $_ =~ /^([^:]*):(\d*): (Error: .*)$/ ||
	     $_ =~ /^([^:]*):(\d*): (Fatal error: .*)$/) {
	$sourceFilename = $1;
	$sourceLineNumber = $2;
	$message = $3;
	if ($message =~ /\((.*)\)$/) {
	    $warningTag = $1;
	} else {
	    $warningTag = "Unknown";
	}
    } elsif ($_ =~ /^(Warning: .*)$/ ||
	     $_ =~ /^(Caution: .*)$/ ||
	     $_ =~ /^(Error: .*)$/ ||
	     $_ =~ /^(Fatal error: .*)$/) {
	$sourceFilename = "";
	$sourceLineNumber = "";
	$message = $1;
	if ($message =~ /\((.*)\)$/) {
	    $warningTag = $1;
	} else {
	    $warningTag = "Unknown";
	}
    } else {
	$_ = <STDIN>;
	next;
    }

    $outSummary = "<LI><A HREF=\"" . filenameFlatten($sourceFilename) . ".html#" .
	"$sourceLineNumber\">" . htmlize($message) . "</A><BR>\n";
    $outCode = "<LI>" . htmlize($message) . "<BR>\n";
    if (! ($currentRoutine =~ /^$/)) {
	$outSummary .= "In class <tt>" . htmlize($currentClass) . "</tt>, routine <tt>";
	$outSummary .= htmlize($currentRoutine) . "</tt><BR>\n";
    }
    $outSummary .= $sourceFilename . ", line " . $sourceLineNumber . ":<BR>\n";
    $warningLine = <STDIN>;
    chop $warningLine;
    $out = "<pre>" . htmlize($warningLine) . "\n";
    $caret = <STDIN>;
    chop $caret;
    $out .= $caret . "\n</pre>\n";
    $_ = <STDIN>;

    if ($_ =~ /^(Associated declaration is )\"([^\"]*)\", line (\d*)(, col \d*:)$/) {
	$out .= "$1<A HREF=\"$2.html#$3\">$2</A>, line $3$4\n";
	$associatedDecl = <STDIN>;
	chop $associatedDecl;
	$out .= "<pre>" . htmlize($associatedDecl) . "\n";
	$caret = <STDIN>;
	chop $caret;
	$out .= $caret . "\n</pre>\n";
	$_ = <STDIN>;
    }

    $suggestion = "None";
    $outSuggestion = "";
    $sourceColumn = "";
    if ($_ =~ /^Suggestion \[\d*,(\d*)\]: none(.*)$/) {
	# ignore this line
	$sourceColumn = $1;
	$_ = <STDIN>;
    } elsif ($_ =~ /^Suggestion \[\d*,(\d*)\]: (.*)$/) {
	$outSuggestion = "Suggestion: " . htmlize($2) . "<p>\n";
	$sourceColumn = $1;
	$suggestion = htmlize($2);
	$_ = <STDIN>;
    }

    $outTrace = "";
    if ($_ =~ /^(Execution trace information:)/) {
	$outTrace .= "$1\n<UL>\n";
	$_ = <STDIN>;
	while (! ($_ =~ /^$/)) {
	    $outTrace .= "$_<BR>\n";
	    $_ = <STDIN>;
	}
	$outTrace .= "</UL><P>\n";
    }

    # Now try to trace back interprocedurally


    $locWarn = "$sourceFilename $sourceLineNumber $sourceColumn";
    $interproctrace = "Interprocedural Trace<UL>" .
	"<LI>" . (makeHrefLoc $locWarn) . htmlize($message) . "</a></LI>\n";
    $t = 100;
    $lastann = "";
    while ($t > 0) {
	$t--;
	$locDecl = $warnDeclLinks{$locWarn};
	if( $declNames{$locDecl} =~ /^(.*) (.*)$/ ) {	    
	    $declType = $1;
	    $declName = $2;
	    $locAnn = $declAnnLinks{$locDecl};
	    if( $locAnn ne "" ) {
		$ann = $annText{$locAnn};
		$reason = "because $declType "
		    . (makeHrefLoc $locDecl) . $declName . "</a> may not have property "
 		    . (makeHrefLoc $locAnn) . "<font>" . htmlize($ann) . "</font></a>";
		$suggestion = $reason;
		$interproctrace .= "<LI> $reason </LI>\n";
		if( $coord{$locAnn} =~ /^(.*)::(.*)$/ ) {
		    $locWarn = $1;
		    $msg = $2;
		    $interproctrace .= "<LI> because of " 
			. (makeHrefLoc $locWarn) . "warning:</a> " . htmlize($msg) . "</LI>\n";
		    next;
		}
	    }
	}
	$t  = 0;
    }
    $interproctrace .= "\n</UL><p>\n";

 #   $interproctrace = "";

    $msgComparable = $message;
    while ( $msgComparable =~ /^(.*)'(.*)'(.*)$/ ) 
    {
	$msgComparable = "$1...$3";
    }

    $outSummaryNoSuggestion = $outSummary . $out . $outTrace . $interproctrace;
    $outSummary .= $out . $outSuggestion . $outTrace . $interproctrace;
    $outCode    .= $out . $outSuggestion . $outTrace . $interproctrace;

    if ($sourceFilename =~ /^$packagePrefix/) {
	push(@$records, [$sourceFilename, $sourceLineNumber, $message, 
			 $outSummary, $outCode, $warningTag, 
			 $suggestion, $outSummaryNoSuggestion,
			 $msgComparable]);
    }

}

###########################
#  Output the warnings page

print STDOUT "<html>\n<head>\n";
print STDOUT "<title>Houdini output</title>\n</head>\n";
print STDOUT "<body bgcolor=\"#ffffdd\">";

print STDOUT "<H1>Houdini output summary</H1>\n<UL>\n";
@fatalErrors = grep($_->[2] =~ /^Fatal error:/, @$records);
if (scalar(@fatalErrors) != 0) {
   print STDOUT "<LI>", scalar(@fatalErrors), 
                " <A HREF=\"#fatalErrors\">fatal errors</A>\n";
} else {
   print STDOUT "<LI>", scalar(@fatalErrors), " fatal errors\n";
}

@errors = grep($_->[2] =~ /^Error:/, @$records);
if (scalar(@errors) != 0) {
   print STDOUT "<LI>", scalar(@errors), 
                " <A HREF=\"#errors\">errors</A>\n";
} else {
   print STDOUT "<LI>", scalar(@errors), " errors\n";
}

@warnings = grep($_->[2] =~ /^Warning:/, @$records);
%warningCounts = ();
%warningClusters = ();
%clusterCounts = ();
$clusters = 0;
foreach $tuple (@warnings) {
    $warningCounts{$tuple->[8]}++;
    if ( $tuple->[6] eq "None" ) {
	$warningClusters{$tuple->[8]}++;
	$clusters++;
    } else {
	$warnSugg = $tuple->[8] . " :SUGGESTION: " . $tuple->[6];
	if ( $clusterCounts{$warnSugg} == 0 ) {
	    # new cluster
	    $warningClusters{$tuple->[8]}++;
	    $clusters++;
	}
	$clusterCounts{$warnSugg}++;
    }
}

print STDOUT "<LI>", scalar(@warnings), 
    " <A HREF=\"#warnings\">warnings</A> in ", 
    $clusters,
    " <A HREF=\"#clusters\">clusters</A>\n";

@cautions = grep($_->[2] =~ /^Caution:/, @$records);
if (scalar(@cautions) != 0) {
   print STDOUT "<LI>", scalar(@cautions), 
                " <A HREF=\"#cautions\">cautions</A>\n";
} else {
   print STDOUT "<LI>", scalar(@cautions), " cautions\n";
}
print STDOUT "</UL>\n";

if (scalar(@fatalErrors) != 0) {
    print STDOUT "<A NAME=\"fatalErrors\"></A>\n<H1>Fatal errors</H1>\n<UL>\n";
    foreach $tuple (@fatalErrors) {
	print STDOUT $tuple->[3], "\n";
    }
    print STDOUT "</UL>\n";
}

if (scalar(@errors) != 0) {
    print STDOUT "<A NAME=\"errors\"></A>\n<H1>Errors</H1>\n<UL>\n";
    foreach $tuple (@errors) {
	print STDOUT $tuple->[3], "\n";
    }
    print STDOUT "</UL>\n";
}

#print STDOUT "<H1>Warning summary</H1>\n";
print STDOUT "<TABLE border=2>\n";
#print STDOUT "<CAPTION>Warning summary</CAPTION>\n";
print STDOUT "<TR><TH>Warning type</TH><TH>Warnings</TH><TH>Clusters</TH></TR>\n";

@arr = sort { $warningCounts{$b} <=> $warningCounts{$a} } keys %warningCounts;
foreach $w (@arr) {
    $w =~ /^Warning: (.*) \((.*)\)$/;
    print STDOUT "<TR><TD><A HREF=\"#$2\">$1 ($2)</A></TD>\n",
                 "<TD align=center>", $warningCounts{$w}, "</TD>\n",
                 "<TD align=center>", $warningClusters{$w}, "</TD></TR>\n";
                 
}
print STDOUT "<TR><TH align=right>Total</TH>",
             "<TD align=center>", scalar(@warnings), "</TD>\n",
             "<TD align=center>", $clusters, "</TD></TR></TABLE\n";
                 
print STDOUT "</UL>\n";
#------------------------

# If html directory has stats.html, insert into STDOUT
if ( $srcHtmlDir ne "" ) {
    if (open(INSERT, $srcHtmlDir . "/stats.html")) {
	while( $line = <INSERT> ) {
	    print STDOUT $line;
	}
    }
}

#----------------------
# Detailed info on clusters, warnings, cautions etc

#--------- Warnings, sorted by cluster

@warningSortedByType =  
    (sort { (($a->[5] cmp $b->[5]) != 0 ? ($a->[5] cmp $b->[5]) :
	     ($a->[0] cmp $b->[0]) != 0 ? ($a->[0] cmp $b->[0]) :
	     ($a->[1] <=> $b->[1])) } @warnings);

print STDOUT "<A NAME=\"clusters\"></A>\n",
             "<H1>Warnings, sorted by cluster</H1>\n<UL>\n";

@arr = sort { $clusterCounts{$b} <=> $clusterCounts{$a} } keys %clusterCounts;
$clusterNumber = 0;
foreach $warnSugg (@arr) {
    $warnSugg =~ /^(.*) :SUGGESTION: (.*)$/;
    $warning = $1;
    $suggestion = $2;
    print STDOUT "<LI><A NAME=\"cluster" . $clusterNumber . "\"></A>\n";
    print STDOUT "<B>Cluster of ", $clusterCounts{$warnSugg}, " warnings</B>";
    if( $clusterNumber != 0 ) {
	print STDOUT " <A HREF=\"#cluster" . ($clusterNumber-1) . "\">prev</A>";
    }
    print STDOUT " <A HREF=\"#cluster" . ($clusterNumber+1) . "\">next</A>";
    print STDOUT "<br>", ($suggestion), "<p><UL>\n";
    foreach $tuple (@warningSortedByType) {
	if ( ($tuple->[6]) eq $suggestion ) {
	    print STDOUT $tuple->[3], "\n";
	}
    }
    print STDOUT "</UL>\n";
    $clusterNumber++;
}
print STDOUT "<LI><A NAME=\"cluster" . $clusterNumber . "\"></A>\n";
print STDOUT "<B>Warnings with no suggestion</B>\n";
if( $clusterNumber != 0 ) {
    print STDOUT " <A HREF=\"#cluster" . ($clusterNumber-1) . "\">prev</A>";
}
print STDOUT "<p><UL>\n";
foreach $tuple (@warningSortedByType) {
    if ( ($tuple->[6]) eq "None" ) {
	print STDOUT $tuple->[3], "\n";
    }
}
print STDOUT "</UL>\n";
print STDOUT "</UL>\n";

#--------- Warnings, sorted by filename

print STDOUT "<A NAME=\"warnings\"></A><H1>Warnings, sorted by filename</H1>\n<UL>\n";
foreach $tuple (@warnings) {
    print STDOUT $tuple->[3], "\n";
}
print STDOUT "</UL>\n";

#--------- Warnings, sorted by  warning message

print STDOUT "<H1>Warnings, sorted by warning message</H1>\n<UL>\n";
$previousWarningTag = "";
foreach $tuple (@warningSortedByType) {
    if ($previousWarningTag ne $tuple->[5]) {
	print STDOUT "<A NAME=\"" . $tuple->[5] . "\"></A>\n";
	$previousWarningTag = $tuple->[5];
    }
    print STDOUT $tuple->[3], "\n";
}
print STDOUT "</UL>\n";

#-------------- cautions

if (scalar(@cautions) != 0) {
    print STDOUT "<A NAME=\"cautions\"></A>\n<H1>Cautions</H1>\n<UL>\n";
    foreach $tuple (@cautions) {
	print STDOUT $tuple->[3], "\n";
    }
    print STDOUT "</UL>\n";
}

print STDOUT "</body>\n</html>\n";

###########################
#  Insert the warnings into the htmlized source code


if ( $srcHtmlDir ne "" && $packagePrefix eq "" ) {

    # sort by (filename, linenumber)
    @$records = sort { (($a->[0] cmp $b->[0]) != 0 ? ($a->[0] cmp $b->[0]) :
			($a->[1] <=> $b->[1]) ) } @$records;

    $i = 0;
    while ($i < scalar(@$records))
    {

	$filename = $records->[$i][0];
	$htmlFilename = filenameFlatten($filename);

	$inname =  $srcHtmlDir . "/" . $htmlFilename . ".html";
	if (! open(IN, $inname)) {
	    print STDERR "Cannot open " . $inname . " for input\n";
	    $i++;
	    next;
	}
	$outname = $srcHtmlDir . "/" . $htmlFilename . ".html.tmp";
	if (! open(OUT, "> " . $outname)) {
	    print STDERR "Cannot open " . $outname . " for output\n";
	    close IN;
	    $i++;
	    next;
	}

	while( $line = <IN> ) {
	    
	    print OUT $line;
	    chop $line;
	    
	    while( ($i < scalar(@$records) &&
		     $filename eq $records->[$i][0] &&
		     $line =~ /^<A NAME="(\d*)">/ &&
		     $1 == @$records->[$i][1] ))
	    {
		# just printed line with the error
		print OUT "</pre><BLOCKQUOTE><UL><font color=\"#cc000\">"
		    . @$records->[$i][4]
		    . "</font></UL></BLOCKQUOTE><pre>";
		$i++;
	    }
        }
	
        # EOF, all done

	if( $i < scalar(@$records) &&
		   $records->[$i][0] eq $filename)
	    {
		# the sort above didn't seem to have done its thang!
		die "Could not insert html at line " . $records->[$i][1]
		    . " into file " . $records->[$i][0];
	    }
	
        close IN;
	close OUT;

	rename $outname, $inname || die "Could not rename " . $outname . " to " . $inname
    }
}
