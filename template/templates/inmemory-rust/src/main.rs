//! インメモリ全書き換えのテンプレート（axum + seed/delta 永続化）
//!
//! 環境変数:
//!   ISU_LISTEN    待ち受け（既定 0.0.0.0:8080）
//!   ISU_SEED_DIR  seed の場所（bin/isu-export-seed の出力。既定 ./seed）
//!   ISU_DELTA     差分ログ（再起動をまたぐので /tmp に置かない。既定 /home/isucon/state/delta.jsonl）
//!   ISU_IDLE_MS   最後の書き込みからこの時間で flush（既定 1000）

mod model;
mod seed;
mod store;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::extract::{Path, State as Ax};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;

use model::{Event, State};
use store::Store;

#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

type App = Arc<Store<State>>;

fn env(k: &str, d: &str) -> String {
    std::env::var(k).unwrap_or_else(|_| d.to_string())
}

fn now_micros() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_micros() as i64
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let seed_dir = PathBuf::from(env("ISU_SEED_DIR", "seed"));
    let delta = env("ISU_DELTA", "/home/isucon/state/delta.jsonl");
    let idle: u64 = env("ISU_IDLE_MS", "1000").parse()?;

    let store: App = Arc::new(Store::open(State::from_seed(&seed_dir)?, &delta)?);
    store.spawn_flusher(Duration::from_millis(idle), 256 << 20);

    let app = Router::new()
        .route("/api/initialize", post(initialize))
        .route("/api/users", post(create_user))
        .route("/api/users/{id}", get(get_user))
        .route("/api/users/{id}/posts", get(list_posts).post(create_post))
        .with_state(store.clone());

    let listener = tokio::net::TcpListener::bind(env("ISU_LISTEN", "0.0.0.0:8080")).await?;
    let s = store.clone();
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            shutdown_signal().await;
            // 接続の後始末を待つ前に書く（TimeoutStopSec 超過で SIGKILL されても残るように）
            if let Err(e) = s.flush() {
                eprintln!("flush on shutdown failed: {e}");
            }
        })
        .await?;
    store.flush()?;
    Ok(())
}

async fn shutdown_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = signal(SignalKind::terminate()).expect("SIGTERM handler");
    tokio::select! {
        _ = term.recv() => {},
        _ = tokio::signal::ctrl_c() => {},
    }
}

// ---------------- handlers（例。当日は問題の API に置き換える） ----------------

async fn initialize(Ax(app): Ax<App>) -> impl IntoResponse {
    match app.reset() {
        Ok(()) => (StatusCode::OK, Json(json!({ "language": "rust" }))).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

#[derive(Deserialize)]
struct CreateUser {
    name: String,
}

async fn create_user(Ax(app): Ax<App>, Json(req): Json<CreateUser>) -> impl IntoResponse {
    let res = app.update(|s| {
        if s.users.values().any(|u| u.name == req.name) {
            return Err(StatusCode::CONFLICT);
        }
        let id = s.next_user_id;
        Ok((id, vec![Event::UserCreated { id, name: req.name.clone(), created_at: now_micros() }]))
    });
    match res {
        Ok(id) => (StatusCode::CREATED, Json(json!({ "id": id }))).into_response(),
        Err(code) => code.into_response(),
    }
}

async fn get_user(Ax(app): Ax<App>, Path(id): Path<i64>) -> impl IntoResponse {
    match app.read().users.get(&id) {
        Some(u) => Json(u.clone()).into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

#[derive(Deserialize)]
struct CreatePost {
    body: String,
}

async fn create_post(Ax(app): Ax<App>, Path(user_id): Path<i64>, Json(req): Json<CreatePost>) -> impl IntoResponse {
    let res = app.update(|s| {
        if !s.users.contains_key(&user_id) {
            return Err(StatusCode::NOT_FOUND);
        }
        let id = s.next_post_id;
        Ok((id, vec![Event::PostCreated { id, user_id, body: req.body.clone(), created_at: now_micros() }]))
    });
    match res {
        Ok(id) => (StatusCode::CREATED, Json(json!({ "id": id }))).into_response(),
        Err(code) => code.into_response(),
    }
}

async fn list_posts(Ax(app): Ax<App>, Path(user_id): Path<i64>) -> impl IntoResponse {
    let s = app.read();
    let posts = s.posts_by_user.get(&user_id).map(|v| v.as_slice()).unwrap_or(&[]);
    // 新しい順に最大20件（ロックを握ったまま重い処理をしない）
    let out: Vec<_> = posts.iter().rev().take(20).cloned().collect();
    drop(s);
    Json(out).into_response()
}
