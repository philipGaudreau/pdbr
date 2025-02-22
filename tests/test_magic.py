import inspect
import os
import re
import sys
from pathlib import Path

import pytest
from rich.console import Console
from rich.theme import Theme

from pdbr._pdbr import rich_pdb_klass

NUMBER_RE = r"[\d.e+_,-]+"  # Matches 1e+03, 1.0e-03, 1_000, 1,000

TAG_RE = re.compile(r"\x1b[\[\]]+[\dDClhJt;?]+m?")


def untag(s):
    """Not perfect, but does the job.
    >>> untag('\x1b[0mfoo\x1b[0m\x1b[0;34m(\x1b[0m\x1b[0marg\x1b[0m\x1b[0;34m)\x1b[0m'
    >>>       '\x1b[0;34m\x1b[0m\x1b[0;34m\x1b[0m\x1b[0m')
    'foo(arg)'
    """
    s = s.replace("\x07", "")
    s = s.replace("\x1b[?2004l", "")
    return TAG_RE.sub("", s)


def unquote(s):
    """
    >>> unquote('"foo"')
    'foo'
    >>> unquote('"foo"bar')
    '"foo"bar'
    """
    for quote in ('"', "'"):
        if s.startswith(quote) and s.endswith(quote):
            return s[1:-1]
    return s


TMP_FILE_CONTENT = '''def foo(arg):
    """Foo docstring"""
    pass
    '''


def import_tmp_file(rpdb, tmp_path: Path, file_content=TMP_FILE_CONTENT) -> Path:
    """Creates a temporary file, writes `file_content` to it and makes pdbr import it"""
    tmp_file = tmp_path / "foo.py"
    tmp_file.write_text(file_content)

    rpdb.onecmd(f'import sys; sys.path.append("{tmp_file.parent.absolute()}")')
    rpdb.onecmd(f"from {tmp_file.stem} import foo")
    return tmp_file


@pytest.fixture
def pdbr_child_process(tmp_path):
    """
    Spawn a pdbr prompt in a child process.
    """
    from pexpect import spawn

    file = tmp_path / "foo.py"
    file.write_text("breakpoint()")
    env = os.environ.copy()
    env["IPY_TEST_SIMPLE_PROMPT"] = "1"

    child = spawn(
        str(Path(sys.executable).parent / "pdbr"),
        [str(file)],
        env=env,
        encoding="utf-8",
    )
    child.expect("foo.py")
    child.expect("breakpoint")
    child.timeout = 10
    return child


@pytest.fixture
def RichIPdb():
    """
    In contrast to the normal RichPdb in test_pdbr.py which inherits from
    built-in pdb.Pdb, this one inherits from IPython's TerminalPdb, which holds
    a 'shell' attribute that is a IPython TerminalInteractiveShell.
    This is required for the magic commands to work (and happens automatically
    when the user runs pdbr when IPython is importable).
    """
    from IPython.terminal.debugger import TerminalPdb

    currentframe = inspect.currentframe()

    def rich_ipdb_klass(*args, **kwargs):
        ripdb = rich_pdb_klass(TerminalPdb, show_layouts=False)(*args, **kwargs)
        # Set frame and stack related self-attributes
        ripdb.botframe = currentframe.f_back
        ripdb.setup(currentframe.f_back, None)
        # Set the console's file to stdout so that we can capture the output
        _console = Console(
            file=kwargs.get("stdout", sys.stdout),
            theme=Theme(
                {"info": "dim cyan", "warning": "magenta", "danger": "bold red"}
            ),
        )
        ripdb._console = _console
        return ripdb

    return rich_ipdb_klass


@pytest.mark.skipif(sys.platform.startswith("win"), reason="pexpect")
@pytest.mark.slow
class TestPdbrChildProcess:
    def test_time(self, pdbr_child_process):
        pdbr_child_process.sendline("from time import sleep")
        pdbr_child_process.sendline("%time sleep(0.1)")
        pdbr_child_process.expect(re.compile("CPU times: .+"))
        pdbr_child_process.expect("Wall time: .+")

    def test_timeit(self, pdbr_child_process):
        pdbr_child_process.sendline("%timeit -n 1 -r 1 pass")
        pdbr_child_process.expect_exact("std. dev. of 1 run, 1 loop each)")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="pexpect")
class TestPdbrMagic:
    def test_onecmd_time_line_magic(self, capsys, RichIPdb):
        RichIPdb().onecmd("%time pass")
        captured = capsys.readouterr()
        output = captured.out
        assert re.search(
            f"CPU times: user {NUMBER_RE} [mµn]s, "
            f"sys: {NUMBER_RE} [mµn]s, "
            f"total: {NUMBER_RE} [mµn]s\n"
            f"Wall time: {NUMBER_RE} [mµn]s",
            output,
        )

    def test_onecmd_unsupported_cell_magic(self, capsys, RichIPdb):
        RichIPdb().onecmd("%%time pass")
        captured = capsys.readouterr()
        output = captured.out
        error = (
            "Cell magics (multiline) are not yet supported. Use a single '%' instead."
        )
        assert output == "*** " + error + "\n"
        cmd = "%%time"
        stop = RichIPdb().onecmd(cmd)
        captured_output = capsys.readouterr().out
        assert not stop
        RichIPdb().error(error)
        cell_magics_error = capsys.readouterr().out
        assert cell_magics_error == captured_output

    def test_onecmd_lsmagic_line_magic(self, capsys, RichIPdb):
        RichIPdb().onecmd("%lsmagic")
        captured = capsys.readouterr()
        output = captured.out

        assert re.search(
            "Available line magics:\n%alias +%alias_magic +%autoawait.*%%writefile",
            output,
            re.DOTALL,
        )

    def test_no_zombie_lastcmd(self, capsys, RichIPdb):
        rpdb = RichIPdb(stdout=sys.stdout)
        rpdb.onecmd("print('SHOULD_NOT_BE_IN_%pwd_OUTPUT')")
        captured = capsys.readouterr()
        assert captured.out.endswith(
            "SHOULD_NOT_BE_IN_%pwd_OUTPUT\n"
        )  # Starts with colors and prompt
        rpdb.onecmd("%pwd")
        captured = capsys.readouterr()
        assert captured.out.endswith(Path.cwd().absolute().as_posix() + "\n")
        assert "SHOULD_NOT_BE_IN_%pwd_OUTPUT" not in captured.out

    def test_IPython_Pdb_magics_implementation(self, tmp_path, capsys, RichIPdb):
        """
        We test do_{magic} methods that are concretely implemented by
        IPython.core.debugger.Pdb, and don't default to IPython's
        'InteractiveShell.run_line_magic()' like the other magics.
        """
        from IPython.utils.text import dedent

        rpdb = RichIPdb(stdout=sys.stdout)
        tmp_file = import_tmp_file(rpdb, tmp_path)

        # pdef
        rpdb.do_pdef("foo")
        do_pdef_foo_output = capsys.readouterr().out
        untagged = untag(do_pdef_foo_output).strip()
        assert untagged.endswith("foo(arg)"), untagged
        rpdb.onecmd("%pdef foo")
        magic_pdef_foo_output = capsys.readouterr().out
        untagged = untag(magic_pdef_foo_output).strip()
        assert untagged.endswith("foo(arg)"), untagged

        # pdoc
        rpdb.onecmd("%pdoc foo")
        magic_pdef_foo_output = capsys.readouterr().out
        untagged = untag(magic_pdef_foo_output).strip()
        expected_docstring = dedent(
            """Class docstring:
            Foo docstring
        Call docstring:
            Call self as a function."""
        )
        assert untagged == expected_docstring, untagged

        # pfile
        rpdb.onecmd("%pfile foo")
        magic_pfile_foo_output = capsys.readouterr().out
        untagged = untag(magic_pfile_foo_output).strip()
        tmp_file_content = Path(tmp_file).read_text().strip()
        assert untagged == tmp_file_content

        # pinfo
        rpdb.onecmd("%pinfo foo")
        magic_pinfo_foo_output = capsys.readouterr().out
        untagged = untag(magic_pinfo_foo_output).strip()
        expected_pinfo = dedent(
            f"""Signature: foo(arg)
        Docstring: Foo docstring
        File:      {tmp_file.absolute()}
        Type:      function"""
        )
        assert untagged == expected_pinfo, untagged

        # pinfo2
        rpdb.onecmd("%pinfo2 foo")
        magic_pinfo2_foo_output = capsys.readouterr().out
        untagged = untag(magic_pinfo2_foo_output).strip()
        expected_pinfo2 = re.compile(
            dedent(
                rf"""Signature: foo\(arg\)
        Source:\s*
        %s
        File:      {tmp_file.absolute()}
        Type:      function"""
            )
            % re.escape(tmp_file_content)
        )
        assert expected_pinfo2.fullmatch(untagged), untagged

        # psource
        rpdb.onecmd("%psource foo")
        magic_psource_foo_output = capsys.readouterr().out
        untagged = untag(magic_psource_foo_output).strip()
        expected_psource = 'def foo(arg):\n    """Foo docstring"""\n    pass'
        assert untagged == expected_psource, untagged

    def test_expr_questionmark_pinfo(self, tmp_path, capsys, RichIPdb):
        from IPython.utils.text import dedent

        rpdb = RichIPdb(stdout=sys.stdout)
        tmp_file = import_tmp_file(rpdb, tmp_path)
        # pinfo
        rpdb.onecmd(rpdb.precmd("foo?"))
        magic_foo_qmark_output = capsys.readouterr().out
        untagged = untag(magic_foo_qmark_output).strip()

        expected_pinfo_path = (
            f"/private/var/folders/.*/{tmp_file.name}"
            if sys.platform == "darwin"
            else f"/tmp/.*/{tmp_file.name}"
        )
        expected_pinfo = re.compile(
            dedent(
                rf""".*Signature: foo\(arg\)
        Docstring: Foo docstring
        File:      {expected_pinfo_path}
        Type:      function"""
            )
        )
        assert expected_pinfo.fullmatch(untagged), f"untagged = {untagged!r}"

        # pinfo2
        rpdb.onecmd(rpdb.precmd("foo??"))
        magic_foo_qmark2_output = capsys.readouterr().out
        rpdb.onecmd(rpdb.precmd("%pinfo2 foo"))
        magic_pinfo2_foo_output = capsys.readouterr().out
        assert magic_pinfo2_foo_output == magic_foo_qmark2_output

    def test_filesystem_magics(self, capsys, RichIPdb):
        cwd = Path.cwd().absolute().as_posix()
        rpdb = RichIPdb(stdout=sys.stdout)
        rpdb.onecmd("%pwd")
        pwd_output = capsys.readouterr().out.strip()
        assert pwd_output == cwd
        rpdb.onecmd("import os; os.getcwd()")
        pwd_output = unquote(capsys.readouterr().out.strip())
        assert pwd_output == cwd

        new_dir = str(Path.cwd().absolute().parent)
        rpdb.onecmd(f"%cd {new_dir}")
        cd_output = untag(capsys.readouterr().out.strip())
        assert cd_output.endswith(new_dir)
        rpdb.onecmd("%pwd")
        pwd_output = capsys.readouterr().out.strip()
        assert pwd_output == new_dir
        rpdb.onecmd("import os; os.getcwd()")
        pwd_output = unquote(capsys.readouterr().out.strip())
        assert pwd_output == new_dir
