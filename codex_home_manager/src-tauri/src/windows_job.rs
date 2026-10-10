#[cfg(windows)]
mod platform {
    use std::{ffi::c_void, io, mem, ptr};

    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE},
        },
    };

    #[derive(Debug)]
    pub struct WindowsJob {
        handle: HANDLE,
    }

    // The handle is owned by this value and Windows job handles can be closed from any thread.
    unsafe impl Send for WindowsJob {}
    unsafe impl Sync for WindowsJob {}

    impl WindowsJob {
        pub fn assign_process(process_id: u32) -> io::Result<Self> {
            unsafe {
                let job = CreateJobObjectW(ptr::null(), ptr::null());
                if job.is_null() {
                    return Err(io::Error::last_os_error());
                }

                let mut information: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = mem::zeroed();
                information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let configured = SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &information as *const _ as *const c_void,
                    mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if configured == 0 {
                    let error = io::Error::last_os_error();
                    CloseHandle(job);
                    return Err(error);
                }

                let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, process_id);
                if process.is_null() {
                    let error = io::Error::last_os_error();
                    CloseHandle(job);
                    return Err(error);
                }
                let assigned = AssignProcessToJobObject(job, process);
                let assignment_error = if assigned == 0 {
                    Some(io::Error::last_os_error())
                } else {
                    None
                };
                CloseHandle(process);
                if let Some(error) = assignment_error {
                    CloseHandle(job);
                    return Err(error);
                }

                Ok(Self { handle: job })
            }
        }
    }

    impl Drop for WindowsJob {
        fn drop(&mut self) {
            unsafe {
                if !self.handle.is_null() {
                    CloseHandle(self.handle);
                }
            }
        }
    }
}

#[cfg(not(windows))]
mod platform {
    use std::io;

    #[derive(Debug)]
    pub struct WindowsJob;

    impl WindowsJob {
        pub fn assign_process(_process_id: u32) -> io::Result<Self> {
            Ok(Self)
        }
    }
}

pub use platform::WindowsJob;
