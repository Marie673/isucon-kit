//! seed（bin/isu-export-seed が書き出す <table>.jsonl）の読み込み

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

use serde::de::DeserializeOwned;

/// <dir>/<table>.jsonl を読む。ファイルがなければ空
pub fn load<T: DeserializeOwned>(dir: &Path, table: &str) -> anyhow::Result<Vec<T>> {
    let path = dir.join(format!("{table}.jsonl"));
    if !path.exists() {
        eprintln!("seed: {} がないので空として扱います", path.display());
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    for (i, line) in BufReader::new(File::open(&path)?).lines().enumerate() {
        let line = line?;
        if line.is_empty() {
            continue;
        }
        out.push(
            serde_json::from_str(&line)
                .map_err(|e| anyhow::anyhow!("{}:{}: {e}", path.display(), i + 1))?,
        );
    }
    Ok(out)
}

/// MySQL の DATETIME 文字列 "YYYY-MM-DD HH:MM:SS[.ffffff]"（UTC とみなす）を epoch マイクロ秒に
pub fn datetime_micros(s: &str) -> i64 {
    let b = s.as_bytes();
    let num = |r: std::ops::Range<usize>| -> i64 { s[r].parse().unwrap_or(0) };
    let (y, m, d) = (num(0..4), num(5..7), num(8..10));
    let (hh, mm, ss) = (num(11..13), num(14..16), num(17..19));
    let mut frac = 0i64;
    if b.len() > 20 && b[19] == b'.' {
        let f = &s[20..b.len().min(26)];
        frac = f.parse::<i64>().unwrap_or(0) * 10i64.pow(6 - f.len() as u32);
    }
    // days from civil (Howard Hinnant)
    let y2 = if m <= 2 { y - 1 } else { y };
    let era = if y2 >= 0 { y2 } else { y2 - 399 } / 400;
    let yoe = y2 - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    ((days * 86400 + hh * 3600 + mm * 60 + ss) * 1_000_000) + frac
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses_mysql_datetime() {
        assert_eq!(datetime_micros("1970-01-01 00:00:00"), 0);
        assert_eq!(datetime_micros("2024-11-24 16:00:58.000000"), 1_732_464_058_000_000);
        assert_eq!(datetime_micros("2000-03-01 00:00:00.5"), 951_868_800_500_000);
    }
}
