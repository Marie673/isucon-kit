use std::env;

fn flags() -> bool {
    let fast = std::env::var("ISU_FAST_FEED").map(|v| v == "1").unwrap_or(false);
    let batch = env::var("ISU_SIG_BATCH").is_ok();
    let other = std::env::var("ISU_UNLISTED_FLAG").is_ok();
    let home = std::env::var("HOME").ok();
    fast && batch && other && home.is_some()
}
