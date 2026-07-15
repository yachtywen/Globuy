"""Chapter 6: write and inject a local long-term preference."""

from pathlib import Path

from app.memory import PreferenceEntry, PreferenceStore, render_memory_context

store = PreferenceStore(Path("output/example-memory"))
store.upsert(
    PreferenceEntry(
        user_id="example-user",
        key="headphone_preference",
        value="偏好轻便、通勤降噪，预算 1000 CNY",
        confidence=0.9,
    )
)
print(render_memory_context(store.list("example-user")))
