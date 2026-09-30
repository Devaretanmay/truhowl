import { createClient } from '@supabase/supabase-js';

const supabase = createClient('https://example.supabase.co', 'key');

export function getCurrentUser() {
  return supabase.auth.user();
}
