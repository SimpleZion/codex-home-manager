#![cfg_attr(not(windows), allow(dead_code))]

#[cfg(windows)]
mod windows {
    use std::env;
    use std::ffi::OsString;
    use std::io;
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::{Command, ExitCode, ExitStatus, Stdio};
    use std::thread;
    use std::time::{Duration, Instant};

    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    const CTRL_C_EVENT: u32 = 0;
    const PROBE_CHILD_ARG: &str = "--codex-appserver-shield-probe-child";
    const SELF_TEST_ARG: &str = "--codex-appserver-shield-self-test";
    const REAL_EXE_ENV: &str = "CODEX_APP_SERVER_REAL_EXE";
    const CODEX_HOME_ENV: &str = "CODEX_HOME";
    const ACTIVE_ENV: &str = "CODEX_APP_SERVER_SHIELD_ACTIVE";

    #[link(name = "Kernel32")]
    extern "system" {
        fn AllocConsole() -> i32;
        fn FreeConsole() -> i32;
        fn GenerateConsoleCtrlEvent(ctrl_event: u32, process_group_id: u32) -> i32;
        fn SetConsoleCtrlHandler(
            handler_routine: Option<unsafe extern "system" fn(u32) -> i32>,
            add: i32,
        ) -> i32;
    }

    fn set_ctrl_c_ignored(ignored: bool) -> io::Result<()> {
        let result = unsafe { SetConsoleCtrlHandler(None, i32::from(ignored)) };
        if result == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    fn real_executable(current_executable: &Path) -> io::Result<PathBuf> {
        let candidate = env::var_os(REAL_EXE_ENV)
            .map(PathBuf::from)
            .or_else(|| {
                env::var_os(CODEX_HOME_ENV).map(|codex_home| {
                    PathBuf::from(codex_home)
                        .join("plugins")
                        .join(".plugin-appserver")
                        .join("codex.exe")
                })
            })
            .unwrap_or_else(|| current_executable.with_file_name("codex-appserver-real.exe"));
        if !candidate.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("real Codex executable not found: {}", candidate.display()),
            ));
        }
        let current = current_executable.canonicalize()?;
        let real = candidate.canonicalize()?;
        if current == real {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "shield and real Codex executable resolve to the same file",
            ));
        }
        Ok(real)
    }

    fn spawn_protected(
        executable: &Path,
        arguments: impl IntoIterator<Item = OsString>,
    ) -> io::Result<ExitStatus> {
        Command::new(executable)
            .args(arguments)
            .env(ACTIVE_ENV, "1")
            .stdin(Stdio::inherit())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .creation_flags(CREATE_NEW_PROCESS_GROUP)
            .status()
    }

    fn exit_code(status: ExitStatus) -> ExitCode {
        match status.code() {
            Some(code) if (0..=255).contains(&code) => ExitCode::from(code as u8),
            Some(code) => {
                eprintln!(
                    "codex-appserver-shield: real Codex exited with Windows status {code:#x}"
                );
                ExitCode::FAILURE
            }
            None => ExitCode::FAILURE,
        }
    }

    fn probe_child() -> ExitCode {
        println!("probe-ready:{}", std::process::id());
        thread::sleep(Duration::from_millis(1_200));
        println!("probe-complete");
        ExitCode::SUCCESS
    }

    fn self_test(current_executable: &Path) -> ExitCode {
        unsafe { FreeConsole() };
        if unsafe { AllocConsole() } == 0 {
            eprintln!(
                "codex-appserver-shield self-test could not allocate an isolated console: {}",
                io::Error::last_os_error()
            );
            return ExitCode::FAILURE;
        }
        if let Err(error) = set_ctrl_c_ignored(true) {
            eprintln!(
                "codex-appserver-shield self-test could not install its Ctrl+C guard: {error}"
            );
            return ExitCode::FAILURE;
        }
        let started = Instant::now();
        let mut child = match Command::new(current_executable)
            .arg(PROBE_CHILD_ARG)
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .creation_flags(CREATE_NEW_PROCESS_GROUP)
            .spawn()
        {
            Ok(child) => child,
            Err(error) => {
                eprintln!("codex-appserver-shield self-test could not start probe: {error}");
                return ExitCode::FAILURE;
            }
        };
        thread::sleep(Duration::from_millis(250));
        let generated = unsafe { GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0) };
        if generated == 0 {
            let error = io::Error::last_os_error();
            let _ = child.kill();
            let _ = child.wait();
            eprintln!("codex-appserver-shield self-test could not generate Ctrl+C: {error}");
            return ExitCode::FAILURE;
        }
        match child.wait() {
            Ok(status) if status.success() && started.elapsed() >= Duration::from_millis(1_000) => {
                println!("codex-appserver-shield self-test passed: protected child survived targeted Ctrl+C");
                ExitCode::SUCCESS
            }
            Ok(status) => {
                eprintln!(
                    "codex-appserver-shield self-test failed: probe status={status}, elapsed_ms={}",
                    started.elapsed().as_millis()
                );
                ExitCode::FAILURE
            }
            Err(error) => {
                eprintln!("codex-appserver-shield self-test could not wait for probe: {error}");
                ExitCode::FAILURE
            }
        }
    }

    pub fn run() -> ExitCode {
        if let Err(error) = set_ctrl_c_ignored(true) {
            eprintln!("codex-appserver-shield could not ignore Ctrl+C: {error}");
            return ExitCode::FAILURE;
        }
        let current_executable = match env::current_exe() {
            Ok(path) => path,
            Err(error) => {
                eprintln!("codex-appserver-shield could not resolve its executable path: {error}");
                return ExitCode::FAILURE;
            }
        };
        let arguments: Vec<OsString> = env::args_os().skip(1).collect();
        if arguments
            .first()
            .is_some_and(|value| value == PROBE_CHILD_ARG)
        {
            return probe_child();
        }
        if arguments
            .first()
            .is_some_and(|value| value == SELF_TEST_ARG)
        {
            return self_test(&current_executable);
        }
        if env::var_os(ACTIVE_ENV).is_some() {
            eprintln!("codex-appserver-shield refused recursive activation");
            return ExitCode::FAILURE;
        }
        let real_executable = match real_executable(&current_executable) {
            Ok(path) => path,
            Err(error) => {
                eprintln!(
                    "codex-appserver-shield could not resolve the real Codex executable: {error}"
                );
                return ExitCode::FAILURE;
            }
        };
        match spawn_protected(&real_executable, arguments) {
            Ok(status) => exit_code(status),
            Err(error) => {
                eprintln!(
                    "codex-appserver-shield could not start the real Codex executable: {error}"
                );
                ExitCode::FAILURE
            }
        }
    }
}

#[cfg(windows)]
fn main() -> std::process::ExitCode {
    windows::run()
}

#[cfg(not(windows))]
fn main() -> std::process::ExitCode {
    eprintln!("codex-appserver-shield is only supported on Windows");
    std::process::ExitCode::FAILURE
}
