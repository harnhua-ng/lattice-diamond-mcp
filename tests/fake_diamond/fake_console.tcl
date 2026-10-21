# Test double for the Diamond Tcl console (diamondc), run by real tclsh.
#
# It reads commands from stdin like the real console in piped mode: it
# evaluates each complete command at global level, prints uncaught errors to
# stderr, and defines just enough of the prj_* commands for the server tests.
# Report files are copied from tests/data so the report parsers see real text.

set ::loaded 0
set ::ldf ""
set ::dev "FakeFamily FAKE-DEV PKG 1 COM FAKE-DEV-1PKG"
set ::syn lse
set ::sources {}
set ::fail_stage ""
set ::no_bitgen 0
array set ::strategy {PROP_MAP_RegRetiming False}
set ::data_dir $env(FAKE_DIAMOND_DATA)

puts "--- Lattice Diamond  Version 9.9.9.9.9"

proc fake_base {} {
    set dir [file join [file dirname $::ldf] impl1]
    file mkdir $dir
    return [file join $dir "[file rootname [file tail $::ldf]]_impl1"]
}

proc require_project {} {
    if {!$::loaded} { return -code error "Project is not loaded." }
}

proc prj_project {sub args} {
    switch -- $sub {
        open {
            set f [lindex $args 0]
            if {$::loaded} { puts "ERROR - Current project is not closed."; return -code error "Failed to open project" }
            if {![file exists $f]} {
                puts "ERROR - The project file '$f' does not exist."
                return -code error "Failed to open project"
            }
            set ::loaded 1; set ::ldf [file normalize $f]
            return $::ldf
        }
        new {
            array set o $args
            set ::loaded 1; set ::ldf [file normalize [file join [pwd] "$o(-name).ldf"]]
            if {[info exists o(-dev)]} { set ::dev "Fam $o(-dev) PKG 1 COM $o(-dev)" }
            return $::ldf
        }
        save { require_project; return }
        close {
            if {[info exists ::env(FAKE_DIAMOND_CLOSE_LOG)]} {
                set f [open $::env(FAKE_DIAMOND_CLOSE_LOG) a]; puts $f "prj_project close"; close $f
            }
            require_project; set ::loaded 0; return
        }
        default { return -code error "unknown subcommand $sub" }
    }
}

proc prj_dev {args} {
    require_project
    if {[lindex $args 0] eq "set"} {
        set part [lindex $args end]
        set ::dev "Fam $part PKG 1 COM $part"
        return $part
    }
    return $::dev
}

proc prj_syn {args} {
    require_project
    if {[lindex $args 0] eq "set"} { set ::syn [lindex $args 1]; return }
    return $::syn
}

proc prj_impl {sub args} { require_project; return }

proc prj_src {sub args} {
    require_project
    if {$sub eq "add"} { foreach a $args { if {[string index $a 0] ne "-"} { lappend ::sources $a } } }
    puts "fake prj_src $sub $args"
    return
}

proc prj_strgy {sub args} {
    require_project
    set args [lsearch -all -inline -not -exact $args -strategy]
    switch -- $sub {
        get_value {
            set name [lindex $args end]
            if {![info exists ::strategy($name)]} { puts "ERROR: Strategy option \"$name\" does not exist."; return -code error "" }
            return $::strategy($name)
        }
        set_value {
            foreach kv $args { lassign [split $kv =] k v; set ::strategy($k) $v }
            return
        }
    }
}

proc prj_run {args} {
    require_project
    if {[llength $args] == 0} { return -code error "Wrong # args" }
    set m [lindex $args 0]
    set task ""
    set i [lsearch $args -task]
    if {$i >= 0} { set task [lindex $args [expr {$i + 1}]] }
    if {[lsearch -exact {Synthesis Translate Map PAR Export} $m] < 0} {
        puts "$m is not a valid milestone name."
        return -code error "Wrong # arg $m"
    }
    puts "fake: running $m $task"
    if {$::fail_stage eq $m} {
        puts "ERROR - fake failure in $m"
        return -code error "Running milestone \"$m\" failed"
    }
    set base [fake_base]
    switch -- $m {
        Synthesis { set f [open $base.srr w]; puts $f "synthesis log"; close $f }
        Map  { file copy -force [file join $::data_dir sample.mrp] $base.mrp }
        PAR  {
            file copy -force [file join $::data_dir sample.par] $base.par
            file copy -force [file join $::data_dir sample_setup_hold.twr] $base.twr
        }
        Export {
            if {$task eq "Bitgen" && !$::no_bitgen} { set f [open $base.bit w]; puts $f "bits"; close $f }
        }
    }
    return
}

proc dmcp_sleep {ms} { after $ms }
proc dmcp_crash {} { exec kill -SEGV [pid]; after 5000 }

set buf ""
while {[gets stdin line] >= 0} {
    append buf $line "\n"
    if {![info complete $buf]} continue
    set cmd $buf
    set buf ""
    if {[catch {uplevel #0 $cmd} msg]} { puts stderr $msg }
    flush stdout
}
