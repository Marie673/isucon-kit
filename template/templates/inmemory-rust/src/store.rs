//! インメモリ状態 + 追記型の差分ログ（delta）による永続化。
//!
//! - 状態の変更はすべて `Event` として表し、`Apply::apply` で状態に反映する（再生しても同じ結果になること）
//! - 変更は書き込みロックの中で「反映 → delta のバッファに積む」ので、delta の順序 = 反映の順序
//! - バッファは「最後の書き込みから `idle` 経過」でファイルに追記して fsync する（ベンチ中は書かない）
//! - SIGTERM（systemd stop / reboot）では main から `flush` を呼ぶ
//! - 起動時: seed から状態を作り、delta を先頭から再生する。途中で切れた最終行は捨てて切り詰める
//! - initialize: 状態を seed に戻し、delta を空にする

use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use parking_lot::{Mutex, RwLock, RwLockReadGuard};
use serde::de::DeserializeOwned;
use serde::Serialize;

pub trait Apply: Clone + Send + Sync + 'static {
    type Event: Serialize + DeserializeOwned + Send + Sync;
    fn apply(&mut self, ev: &Self::Event);
}

pub struct Store<S: Apply> {
    state: RwLock<S>,
    seed: S,
    path: PathBuf,
    // ロック順序は必ず file -> pending（flush と reset の競合で古いイベントが残らないように）
    file: Mutex<File>,
    pending: Mutex<Vec<u8>>,
    last_write_ms: AtomicU64,
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
}

impl<S: Apply> Store<S> {
    /// seed から状態を作り、delta を再生して開く
    pub fn open(seed: S, delta: impl AsRef<Path>) -> anyhow::Result<Self> {
        let path = delta.as_ref().to_path_buf();
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        let mut state = seed.clone();
        let mut good_len: u64 = 0;
        let mut replayed = 0usize;
        if path.exists() {
            let mut r = BufReader::new(File::open(&path)?);
            let mut line = String::new();
            loop {
                line.clear();
                let n = r.read_line(&mut line)?;
                if n == 0 {
                    break;
                }
                if !line.ends_with('\n') {
                    eprintln!("delta: 末尾の不完全な行を捨てます ({n} bytes)");
                    break;
                }
                match serde_json::from_str::<S::Event>(line.trim_end()) {
                    Ok(ev) => {
                        state.apply(&ev);
                        good_len += n as u64;
                        replayed += 1;
                    }
                    Err(e) => {
                        eprintln!("delta: 壊れた行で再生を止めます: {e}");
                        break;
                    }
                }
            }
        }
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        if file.metadata()?.len() != good_len {
            file.set_len(good_len)?;
        }
        eprintln!("store: delta {replayed} 件を再生 ({})", path.display());
        Ok(Self {
            state: RwLock::new(state),
            seed,
            path,
            file: Mutex::new(file),
            pending: Mutex::new(Vec::with_capacity(1 << 20)),
            last_write_ms: AtomicU64::new(now_ms()),
        })
    }

    /// 読み取り（複数同時に可）
    pub fn read(&self) -> RwLockReadGuard<'_, S> {
        self.state.read()
    }

    /// 検証してから変更する。クロージャは書き込みロックの中で呼ばれ、
    /// 返したイベントが順に反映・記録される。Err なら何も変わらない。
    pub fn update<R, E>(
        &self,
        f: impl FnOnce(&S) -> Result<(R, Vec<S::Event>), E>,
    ) -> Result<R, E> {
        let mut st = self.state.write();
        let (r, evs) = f(&st)?;
        if !evs.is_empty() {
            let mut buf = self.pending.lock();
            for ev in &evs {
                st.apply(ev);
                serde_json::to_writer(&mut *buf, ev).expect("event serialize");
                buf.push(b'\n');
            }
            self.last_write_ms.store(now_ms(), Ordering::Relaxed);
        }
        Ok(r)
    }

    /// 検証の要らない変更
    pub fn commit(&self, ev: S::Event) {
        let _ = self.update(|_| Ok::<_, ()>(((), vec![ev])));
    }

    /// initialize: seed に戻し、delta を空にする
    pub fn reset(&self) -> io::Result<()> {
        let mut st = self.state.write();
        let file = self.file.lock();
        self.pending.lock().clear();
        file.set_len(0)?;
        file.sync_all()?;
        *st = self.seed.clone();
        Ok(())
    }

    /// バッファをファイルに追記して fsync
    pub fn flush(&self) -> io::Result<()> {
        let mut file = self.file.lock();
        let buf = std::mem::take(&mut *self.pending.lock());
        if buf.is_empty() {
            return Ok(());
        }
        file.write_all(&buf)?;
        file.sync_data()?;
        Ok(())
    }

    /// 最後の書き込みから `idle` 経過したら flush するスレッドを起動する。
    /// バッファが `max_bytes` を超えたらアイドルを待たずに書く（メモリ保護）。
    pub fn spawn_flusher(self: &Arc<Self>, idle: Duration, max_bytes: usize) {
        let me = Arc::clone(self);
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(100));
            let len = me.pending.lock().len();
            if len == 0 {
                continue;
            }
            let quiet = now_ms().saturating_sub(me.last_write_ms.load(Ordering::Relaxed));
            if quiet >= idle.as_millis() as u64 || len >= max_bytes {
                if let Err(e) = me.flush() {
                    eprintln!("delta flush failed ({}): {e}", me.path.display());
                }
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Clone, Default, Debug, PartialEq)]
    struct Counter {
        n: i64,
        log: Vec<i64>,
    }
    #[derive(Serialize, Deserialize)]
    struct Add(i64);
    impl Apply for Counter {
        type Event = Add;
        fn apply(&mut self, ev: &Add) {
            self.n += ev.0;
            self.log.push(ev.0);
        }
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("isu-mem-test-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        d.join("delta.jsonl")
    }

    #[test]
    fn replay_after_flush() {
        let p = tmp("replay");
        let s = Store::open(Counter::default(), &p).unwrap();
        s.commit(Add(1));
        s.commit(Add(2));
        s.flush().unwrap();
        s.commit(Add(100)); // flush されていない分は失われる（idle flush / SIGTERM flush の役割）
        drop(s);
        let s = Store::open(Counter::default(), &p).unwrap();
        assert_eq!(s.read().n, 3);
        assert_eq!(s.read().log, vec![1, 2]);
    }

    #[test]
    fn update_rejects_atomically() {
        let p = tmp("reject");
        let s = Store::open(Counter::default(), &p).unwrap();
        let r: Result<(), &str> = s.update(|st| if st.n < 0 { Ok(((), vec![Add(1)])) } else { Err("nope") });
        assert!(r.is_err());
        assert_eq!(s.read().n, 0);
    }

    #[test]
    fn reset_clears_delta() {
        let p = tmp("reset");
        let s = Store::open(Counter { n: 10, log: vec![] }, &p).unwrap();
        s.commit(Add(5));
        s.flush().unwrap();
        s.reset().unwrap();
        s.commit(Add(7));
        s.flush().unwrap();
        drop(s);
        let s = Store::open(Counter { n: 10, log: vec![] }, &p).unwrap();
        assert_eq!(s.read().n, 17);
    }

    #[test]
    fn truncated_tail_is_dropped() {
        let p = tmp("tail");
        let s = Store::open(Counter::default(), &p).unwrap();
        s.commit(Add(1));
        s.flush().unwrap();
        drop(s);
        let mut f = OpenOptions::new().append(true).open(&p).unwrap();
        f.write_all(b"[4").unwrap(); // 書き込み途中でクラッシュした行
        drop(f);
        let s = Store::open(Counter::default(), &p).unwrap();
        assert_eq!(s.read().n, 1);
        s.commit(Add(2));
        s.flush().unwrap();
        drop(s);
        let s = Store::open(Counter::default(), &p).unwrap();
        assert_eq!(s.read().n, 3);
    }
}
