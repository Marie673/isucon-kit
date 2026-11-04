//! ドメインモデル（例）。当日はここを問題に合わせて書き換える。
//!
//! 書き換えの手順:
//! 1. seed の行の型（`*Row`）を docs/inventory/schema.sql から書く
//! 2. `State` にメモリ上の構造（主キーのマップ + 引きたい順の索引）を持たせる
//! 3. 状態を変える操作を `Event` として列挙し、`apply` で反映する（再生しても同じ結果になること）
//!    - ID・時刻などの非決定的な値は、イベントを作る側（ハンドラ）で決めてイベントに入れる

use std::collections::HashMap;
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::seed;
use crate::store::Apply;

#[derive(Deserialize)]
struct UserRow {
    id: i64,
    name: String,
    created_at: String,
}

#[derive(Clone, Serialize)]
pub struct User {
    pub id: i64,
    pub name: String,
    pub created_at: i64,
}

#[derive(Clone, Serialize)]
pub struct Post {
    pub id: i64,
    pub user_id: i64,
    pub body: String,
    pub created_at: i64,
}

#[derive(Clone, Default)]
pub struct State {
    pub users: HashMap<i64, User>,
    pub posts_by_user: HashMap<i64, Vec<Post>>,
    pub next_user_id: i64,
    pub next_post_id: i64,
}

#[derive(Serialize, Deserialize)]
#[serde(tag = "t")]
pub enum Event {
    UserCreated { id: i64, name: String, created_at: i64 },
    PostCreated { id: i64, user_id: i64, body: String, created_at: i64 },
}

impl Apply for State {
    type Event = Event;
    fn apply(&mut self, ev: &Event) {
        match ev {
            Event::UserCreated { id, name, created_at } => {
                self.users.insert(*id, User { id: *id, name: name.clone(), created_at: *created_at });
                self.next_user_id = self.next_user_id.max(id + 1);
            }
            Event::PostCreated { id, user_id, body, created_at } => {
                self.posts_by_user.entry(*user_id).or_default().push(Post {
                    id: *id,
                    user_id: *user_id,
                    body: body.clone(),
                    created_at: *created_at,
                });
                self.next_post_id = self.next_post_id.max(id + 1);
            }
        }
    }
}

impl State {
    pub fn from_seed(dir: &Path) -> anyhow::Result<Self> {
        let mut s = State { next_user_id: 1, next_post_id: 1, ..Default::default() };
        for r in seed::load::<UserRow>(dir, "users")? {
            s.apply(&Event::UserCreated {
                id: r.id,
                name: r.name,
                created_at: seed::datetime_micros(&r.created_at),
            });
        }
        Ok(s)
    }
}
