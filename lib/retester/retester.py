import subprocess, shlex, signal, os, time, shutil, tempfile, sys, traceback, types
from vcoptparse import *

reports = []

class SmartTemporaryFile(object):
    """tempfile.NamedTemporaryFile is poorly designed. A very common use case for a temporary file
    is to write some things into it, then close it, but want to leave it on the disk so it can be
    passed to something else. However, tempfile.NamedTemporaryFile either destroys its file as soon
    as it is closed or never destroys it at all. SmartTemporaryFile destroys its file only when it
    is GCed, unless take_file() is called, in which case it never destroys its file at all."""
    
    def __init__(self, *args, **kwargs):
        
        assert "delete" not in kwargs
        kwargs["delete"] = False
        
        # Clearly identify retest2 output files so that we can tell where they are coming from.
        kwargs["prefix"] = "rt2_" + kwargs.get("prefix", "u_")
        
        self.file = tempfile.NamedTemporaryFile(*args, **kwargs)
        self.need_to_delete = True
    
    def take_file(self):
        """By calling take_file(), the caller takes responsibility for destroying the temporary
        file."""
        if not self.need_to_delete:
            raise ValueError("take_file() called twice.")
        self.need_to_delete = False
        return self.name
    
    def __del__(self):
        if hasattr(self, "need_to_delete") and self.need_to_delete:
            os.remove(self.name)
    
    # Forward everything to our internal NamedTemporaryFile object
    def __getattr__(self, name):
        return getattr(self.file, name)

class SmartTemporaryDirectory(object):
    """SmartTemporaryDirectory() creates a temporary directory and destroys it when the
    SmartTemporaryDirectory object is garbage-collected."""
    
    def __init__(self, prefix = "u_"):
        
        self.path = tempfile.mkdtemp(prefix = "rt2_" + prefix)
        assert os.path.isdir(self.path)
        self.need_to_delete =True
    
    def take_dir(self):
        """By calling take_dir(), the caller takes responsibility for destroying the temporary
        directory."""
        if not self.need_to_delete:
            raise ValueError("take_dir() called twice.")
        self.need_to_delete = False
        return self.path
    
    def __del__(self):
        if hasattr(self, "need_to_delete") and self.need_to_delete:
            shutil.rmtree(self.path)

class Result(object):
    """The Result class represents the result of a test. It is either a pass or a fail; if it is a
    fail, then it includes a string description."""
    
    def __init__(self, result, description=None):
        
        assert result in ["pass", "fail"]
        
        if result == "pass":
            self.result = "pass"
            assert description is None
        
        elif result == "fail":
            self.result = "fail"
            assert description is not None
            self.description = str(description)
            self.output_dir = None

def format_exit_code(code):
    """If the exit code is positive, return it in string form. If it is negative, try to figure out
    which signal it corresponds to."""
    
    if code < 0:
        for name in dir(signal):
            if name.startswith("SIG") and -getattr(signal, name) == code:
                return "signal %s" % name
    return "exit code %d" % code

poll_interval = 0.1

output_dir_name = "output_from_test"

def run_test(command, timeout = None):
    """run_test() runs the given command and returns a Result object."""
    
    cwd = os.path.abspath(os.getcwd())
    
    # Remove the old output directory if there is one
    output_dir = os.path.join(cwd, output_dir_name)
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    
    # Capture the command's output
    output = SmartTemporaryFile()
    
    # Instruct the command to put any temp files in a new temporary directory in case it is bad at
    # cleaning up after itself
    temp_dir = SmartTemporaryDirectory(prefix = "tmp_")
    environ = dict(os.environ)
    environ["TMP"] = temp_dir.path
    environ["PYTHONUNBUFFERED"] = "1"
    
    process = subprocess.Popen(
        command,
        stdout = output,
        stderr = subprocess.STDOUT,
        cwd = cwd,
        # Make a new session to make sure that the test doesn't spam our controlling terminal for
        # any reason
        preexec_fn = os.setsid,
        shell = True,
        env = environ
        )
    try:
        if timeout is None:
        
            process.wait()
            if process.returncode == 0:
                result = Result("pass")
            else:
                result = Result("fail", "%r exited with %s." % \
                    (command, format_exit_code(process.returncode)))
                    
        else:
            # Wait 'timeout' seconds and see it if it dies on its own
            for i in xrange(int(timeout / poll_interval) + 1):
                time.sleep(poll_interval)
                
                if process.poll() is not None:
                    # Cool, it died on its own.
                    if process.returncode == 0:
                        result = Result("pass")
                    else:
                        result = Result("fail", "%r exited with %s." % \
                            (command, format_exit_code(process.returncode)))
                    break
            
            # Uh-oh, the timeout elapsed and it's still alive
            else:
            
                # First try to kill it nicely with SIGINT. This will give it a chance to shut down
                # smoothly on its own. There might be data in the output buffers it would be nice
                # to recover; also, it might have resources to clean up or subprocess of its own
                # to kill.
                try: process.send_signal(signal.SIGINT)
                except OSError: pass
                time.sleep(1)
                
                if process.poll() is not None:
                    # SIGINT worked.
                    result = Result("fail", "%r failed to terminate within %g seconds, but " \
                        "exited with %s after being sent SIGINT." % \
                        (command, timeout, format_exit_code(process.poll())))
                
                else:
                    # SIGINT didn't work, escalate to SIGQUIT
                    try: process.send_signal(signal.SIGQUIT)
                    except OSError: pass
                    time.sleep(1)
                    
                    if process.poll() is not None:
                        result = Result("fail", "%r failed to terminate within %g seconds and " \
                            "did not respond to SIGINT, but exited with %s after being sent " \
                            "SIGQUIT." % (command, timeout, format_exit_code(process.poll())))
                    
                    else:
                        # SIGQUIT didn't work either. We'll have to use SIGKILL, and we direct it at
                        # the entire process group (because our immediate child probably isn't going
                        # to be able to clean up after itself)
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except OSError: pass
                        time.sleep(1)
                        
                        if process.poll() is not None:
                            result = Result("fail", "%r failed to terminate within %g seconds and " \
                                "did not respond to SIGINT or SIGQUIT." % (command, timeout))
                        
                        else:
                            # I don't expect this to ever happen
                            result = Result("fail", "%r failed to terminate within %g seconds and " \
                                "did not respond to SIGINT or SIGQUIT. Even SIGKILL had no " \
                                "apparent effect against this monster. I recommend you terminate " \
                                "it manually, because it's probably still rampaging through your " \
                                "system." % (command, timeout))
    
    finally:
        # In case we ourselves receive SIGINT, or there is an exception in the above code, or we
        # terminate our immediate child process and it dies without killing the grandchildren.
        try: os.killpg(process.pid, signal.SIGKILL)
        except OSError: pass
    
    output.close()
    
    if result.result == "fail":
        # Include the output directory with the error message
        new_output_dir = SmartTemporaryDirectory("out_")
        result.output_dir = new_output_dir
        
        if os.path.isdir(output_dir):
            # Replace the original directory that the SmartTemporaryDirectory created with our own
            # directory, but the SmartTemporaryDirectory will still be responsible for deleting it
            os.rmdir(new_output_dir.path)
            os.rename(output_dir, new_output_dir.path)
        
        # Put the output from the command into said directory as well
        os.rename(output.take_file(), os.path.join(new_output_dir.path, "test_output.txt"))
    
    else:
        # Delete the output directory
        if os.path.isdir(output_dir):
            shutil.rmtree(output_dir)
        
    return result

def do_test(cmd, cmd_args, cmd_format="gnu", repeat=1, timeout=60):
    global reports
    
    # Build up the command line
    command = cmd
    for arg in cmd_args:
        command += " "
        # GNU cmd line builder
        if cmd_format == "gnu":
            if(isinstance(cmd_args[arg], types.BooleanType)):
                if cmd_args[arg]:
                    command += "--%s" % arg
            else:
                command += "--%s %s" % (arg, str(cmd_args[arg]))
        # Make cmd line builder
        elif cmd_format == "make":
            command += "%s=%s" % (arg, str(cmd_args[arg]))
        # Invalid cmd line builder
        else:
            print "Invalid command line formatter"
            raise NameError()
    
    # Run the test
    if repeat == 1: print "Running %r..." % command
    else: print "Running %r (repeating %d times)..." % (command, repeat)
    if timeout > 60: print "(This test may take up to %d seconds each time.)" % timeout
        
    results = []
    for i in xrange(repeat):
        results.append(run_test(command, timeout = timeout))
        
    reports.append((command, results))

retest_output_dir = os.path.expanduser("~/retest_output")
retest_output_dir_subdir_lifetime = 60 * 60 * 24   # Output expires after 24 hours

def process_output_file(path):
    """Examine the file and return:
       *  ("ok", contents of file) if the file is small and ASCII
       *  ("big", head of file, count of omitted bytes, tail of file) if the file is large and ASCII
       *  ("other", description) if the file is empty, is not ASCII, is a directory, etc.
    """
    
    # Check for a directory
    
    if os.path.isdir(path):
        return ("other", "directory with %d files/subdirs" % len(os.listdir(path)))
    
    # Check if the file is binary
    
    text = file(path).read(1024)
    unprintables = set(chr(x) for x in xrange(0, 31))
    unprintables -= set("\r\n\t\v")
    unprintables.add(chr(127))
    if any(char in unprintables for char in text):
        return ("other", "binary file")
    
    # The file is text, but it might be too big to include in its entirety
    
    max_size = 10000
    max_newlines = 100
    
    # Open in binary mode so we can use seek(). We're assuming there's nothing funny going on with
    # newlines.
    with file(path, "rb") as f:
        
        f.seek(0, os.SEEK_END)
        length = f.tell()
        if length < max_size:
            f.seek(0, os.SEEK_SET)
            text = f.read()
        
        if length == 0:
            return ("other", "empty file")
        
        elif length < max_size and text.count("\n") < max_newlines:
            # If the file is small enough, we can use the whole thing
            return ("ok", text)
        
        else:
            # Read the beginning of the file
            f.seek(0, os.SEEK_SET)
            beginning = f.read(max_size / 2)
            if beginning.count("\n") < max_newlines / 2:
                # Try to end on a newline
                last_newline = beginning.rfind("\n")
                if last_newline != -1 and last_newline > max_size / 2 - 200:
                    beginning = beginning[:last_newline]
            else:
                beginning = "\n".join(beginning.split("\n")[: max_newlines / 2])
            
            # Read the ending of the file
            f.seek(- max_size / 2, os.SEEK_END)
            ending = f.read(max_size / 2)
            if ending.count("\n") < max_newlines / 2:
                # Try to begin on a newline
                first_newline = ending.find("\n")
                if first_newline != -1 and first_newline < 200:
                    ending = ending[first_newline+1:]
            else:
                ending = "\n".join(ending.split("\n")[- max_newlines / 2 :])
            
            bytes_omitted = length - len(beginning) - len(ending)
            
            return ("big", beginning, "%d bytes" % bytes_omitted, ending)

def process_output_dir(result):
    """Given a Result object, consider its output directory. Copy its output directory into
    retest_output_dir. Also, examine every file in the output directory to see if it is small and
    friendly enough to print to the user. Return a pair with the path to where the output directory
    has been moved and a list of all of the files in the output directory, where each thing in this
    list is a pair of (path, description) where description is the return value of
    process_output_file.
    """
    
    # Create retest_output_dir if necessary
    if not os.path.isdir(retest_output_dir):
        os.mkdir(retest_output_dir)
    
    # Expire old things in retest_output_dir
    for old_dir_name in os.listdir(retest_output_dir):
        old_dir = os.path.join(retest_output_dir, old_dir_name)
        mod_time = os.stat(old_dir)[8]
        if time.time() - mod_time > retest_output_dir_subdir_lifetime:
            shutil.rmtree(old_dir)
    
    # Pick a name for our newest addition to retest_output_dir and copy the directory there
    i = 1
    while os.path.exists(os.path.join(retest_output_dir, str(i))): i += 1
    output_dir = os.path.join(retest_output_dir, str(i))
    os.rename(result.output_dir.take_dir(), output_dir)
    
    # Make a generator that scans all the files in the directory
    def walker():
        for filename in sorted(os.listdir(output_dir)):
            path = os.path.join(output_dir, filename)
            yield (path, process_output_file(path))
    
    return (output_dir, walker())

def count_failures(tests):
    failures = 0
    for (name, results) in tests:
        if any(result.result == "fail" for result in results):
            failures += 1
    return failures

def count_sub_failures(results):
    failures = 0
    for result in results:
        if result.result == "fail":
            failures += 1
    return failures

def print_results_as_plaintext(opts, tests):
    """Given a list of pairs of (command, result), print the results to the shell."""
    
    istr = " " * 2
    def indent(text, istr = istr):
        return istr + text.replace("\n", "\n" + istr)
    
    if opts["header"]:
        print opts["header"]
        print
    print "Out of %d tests, %d passed and %d failed." % \
        (len(tests), len(tests) - count_failures(tests), count_failures(tests))
    print
    
    for (name, results) in tests:
        sub_failures = count_sub_failures(results)
        if sub_failures == 0: print "Passed: %s" % name
        elif sub_failures == len(results): print "Failed: %s" % name
        else: print "Failed (intermittently): %s" % name
    print
    
    for (name, results) in tests:
        
        if count_sub_failures(results) == 0: continue
        
        print "-" * 48
        print
        
        print "Command:", name
        
        if len(results) == 1:
            print "Failed. One run was performed."
        else:
            print "Failed on %d out of %d runs." % (count_sub_failures(results), len(results))
        print
        
        have_printed_verbose_output = False
        
        for i, result in enumerate(results):
            i += 1   # Convert from 0-indexing to 1-indexing
            
            if result.result == "pass":
                print "Run #%d:" % i, "Passed." 
                print
                
            else:
                print "Run #%d:" % i, result.description
                print
            
                if result.output_dir is not None:
                    (output_dir, output_files) = process_output_dir(result)
                    print "Output from run #%d was put in %r." % (i, output_dir)
                    print
                    
                    # Only for one of the runs do we print a full listing of the files, because
                    # the information is probably redundant.
                    if not have_printed_verbose_output:
                        have_printed_verbose_output = True
                        
                        print "Listing of files in output dir from run #%d:" % i
                        print
                        for (name, report) in output_files:
                            if report[0] == "ok":
                                print istr + "%s:" % name
                                print
                                print indent(indent(report[1].strip()))
                                print
                            elif report[0] == "big":
                                (head, omitted, tail) = report[1:]
                                print
                                print istr + "%s:" % name
                                print indent(indent(head.strip()))
                                print
                                print istr + istr + "(omitted %s)" % omitted
                                print
                                print indent(indent(tail.strip()))
                                print
                            elif report[0] == "other":
                                print istr + "%s: (%s)" % (name, report[1])
                            else:
                                raise ValueError("expected 'big', 'binary', 'ok', or 'empty'")
        
        print

def print_results_as_html(opts, tests):
    """Given a list of pairs of (command, result), print the results to the shell in HTML format."""
    
    def escape(string):
        return string.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    def code(string):
        return """<code>%s</code>""" % escape(string).replace("\n", "<br>")
    
    # Header div
    
    print """<div style="border-top: solid 5px black; padding: 0; margin-bottom: 0.3cm">"""
    print """<div style="padding: 0.3cm; border: solid 1px gray; margin-bottom: 0.25cm">"""
    
    if opts["header"]:
        print """<p>%s</p>""" % escape(opts["header"]).replace("\n", """<br/>""")
    
    print """<p>Out of <span style="font-weight: bold">%d</span> tests,
                <span style="font-weight: bold; color: green">%d</span> passed and
                <span style="font-weight: bold; color: red">%d</span> failed.</p>""" % \
        (len(tests), len(tests) - count_failures(tests), count_failures(tests))
    
    print """<table>"""
    for (name, results) in tests:
        print """<tr>"""
        sub_failures = count_sub_failures(results)
        if sub_failures == 0:
            print """<td>%s</td><td><span style="color: green">Passed</span></td>""" % code(name)
        else:
            if sub_failures == len(results): msg = "Failed"
            else: msg = "Failed (intermittently)"
            print """<td>%s</td><td><span style="color: red">%s</span></td>""" % (code(name), msg)
        print """</tr>"""
    print """</table>"""
    
    print """</div>"""
    print """</div>"""
    
    # A div for each failed test
    
    for name, results in tests:
        
        sub_failures = count_sub_failures(results)
        
        if sub_failures == 0: continue
        
        print """<div style="border-top: solid 5px black; padding: 0; margin-bottom: 0.3cm">"""
        
        # A div for the header
        
        print """<div style="border: solid 1px gray; padding: 0.3cm">"""
        print """<p><b>Command:</b> %s</p>""" % code(name)
        
        if len(results) == 1:
            print """<p>Failed. One run was performed.</p>"""
        else:
            print """<p>Failed on %d out of %d runs.</p>""" % (sub_failures, len(results))
        print
        
        print """</div>"""
        
        # A div for each run of the test
        
        have_printed_verbose_output = False
        
        for i, result in enumerate(results):
            i += 1   # Convert from 0-indexing to 1-indexing
            
            print """<div style="border: solid 1px gray; border-top: none; padding: 0.3cm">"""
            
            if result.result == "pass":
                print """<p><b>Run #%d:</b> <span style="color: green">Passed</span></p>""" % i
                
            else:
                print """<p><b>Run #%d:</b> <span style="color: red">Failed</span>. %s</p>""" % \
                    (i, code(result.description))
            
                if result.output_dir is not None:
                    (output_dir, output_files) = process_output_dir(result)
                    print """<p>Output from run #%d was put in %s.</p>""" % (i, code(output_dir))
                    
                    # Only for one of the runs do we print a full listing of the files, because
                    # the information is probably redundant.
                    if not have_printed_verbose_output:
                        have_printed_verbose_output = True
                        
                        print """<p>Listing of files in output dir from run #%d:</p>""" % i
                        print
                        for (name, report) in output_files:
                            assert report[0] in ["big", "ok", "other"]
                            if report[0] == "other":
                                print """<p>%s: (%s)</p>""" % (code(name), report[1])
                            else:
                                print """<p>%s:</p>""" % code(name)
                                print """<div style="border: dashed 1px; padding-left: 0.5cm">"""
                                if report[0] == "ok":
                                    print """<pre>%s</pre>""" % escape(report[1].strip())
                                elif report[0] == "big":
                                    (head, omitted, tail) = report[1:]
                                    print """<pre>%s</pre>""" % escape(head.strip())
                                    print """<p>(omitted %s)</p>""" % omitted
                                    print """<pre>%s</pre>""" % escape(tail.strip())
                                print """</div>"""
            
            print """</div>"""
            
        print """</div>"""

def send_email(opts, message, recipient):
    
    print "Sending email to %r..." % recipient
    
    num_tries = 10
    try_interval = 10   # Seconds
    smtp_server, smtp_port = os.environ.get("RETESTER_SMTP", "smtp.gmail.com:587").split(":")
    
    import smtplib

    for tries in range(num_tries):
        try:
            s = smtplib.SMTP(smtp_server, smtp_port)
        except socket.gaierror:
            # Network is being funny. Try again.
            time.sleep(try_interval)
        else:
            break
    else:
        raise Exception("Cannot connect to SMTP server '%s'" % smtp_server)
    
    sender, sender_pw = os.environ["RETESTER_EMAIL_SENDER"].split(":")
    
    s.starttls()
    s.login(sender, sender_pw)
    s.sendmail(sender, [recipient], message.as_string())
    s.quit()
    
    print "Email message sent."

def send_results_by_email(opts, tests, recipient):
    
    import email.mime.text, cStringIO
    
    email_format = os.environ.get("RETESTER_EMAIL_FORMAT", "html")
    if email_format == "plaintext":
        printer = print_results_as_plaintext
        mime_type = "plain"
    elif email_format == "html":
        printer = print_results_as_html
        mime_type = "html"
    else:
        raise ValueError("RETESTER_EMAIL_FORMAT should be 'html' or 'plaintext'")
    
    sys.stdout = stringio = cStringIO.StringIO()
    try: printer(opts, tests)
    finally: sys.stdout = sys.__stdout__
    message = email.mime.text.MIMEText(stringio.getvalue(), mime_type)
    stringio.close()
    
    subject = "Test results: %d pass, %d fail" % \
        (len(tests) - count_failures(tests), count_failures(tests))
    message.add_header("Subject", subject)
    
    send_email(opts, message, recipient)

def report():
    # Parse arguments
    op = OptParser()
    class TargetArg(Arg):
        def __init__(self):
            self.flags = ["--email", "--print"]
            self.default = [("print", )]
            self.name = "method of reporting results"
            self.combiner = append_combiner
        def flag(self, flag, args):
            if flag == "--email":
                try:
                    recipient = args.pop(0)
                    assert "@" in recipient
                except IndexError, AssertionError:
                    raise OptError("'--email' should be followed by recipient's email address.")
                return ("email", recipient)
            else:
                return ("print", )
    op["targets"] = TargetArg()
    op["header"] = StringFlag("--header", default = None)
    op["tests"] = ManyPositionalArgs()
    
    try:
        opts = op.parse(sys.argv)
    except OptError, e:
        print "Usage: %s [--email RECIPIENT | --print] TESTS" % sys.argv[0]
        print str(e)
        sys.exit(-1)
    if not opts["targets"]:
        print "No targets specified. Oops?"
        sys.exit(1)
    
    # Report the results
    for target in opts["targets"]:
        if target[0] == "print":
            print_results_as_plaintext(opts, reports)
        elif target[0] == "email":
            send_results_by_email(opts, reports, target[1])
        else:
            assert False
    
    # Indicate failure via our exit code
    if count_failures(reports) > 0:
        sys.exit(1)

