// Copy to config.js to give every visitor of your deployment live data.
// Use the publishable key (sb_publishable_…) or the older anon key. Both are
// read-only here (see db/schema.sql), so they are safe to publish.
window.RADAR_CONFIG = {
  supabaseUrl: "https://YOUR-PROJECT.supabase.co",
  anonKey: "sb_publishable_...",
};
