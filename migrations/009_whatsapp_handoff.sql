-- 009_whatsapp_handoff.sql
-- Handoff bot ↔ humano para WhatsApp (y demás canales).
--
-- `bot_activo`: si es FALSE, Sofía NO responde en esa conversación (la atiende un
-- humano, p. ej. Lily). Default TRUE → el comportamiento actual no cambia: todas
-- las conversaciones siguen con el bot activo hasta que un humano tome el control
-- o marquemos a un contacto como "solo humano".
--
-- `atendido_por`: quién respondió por última vez ('bot' | 'humano'), para la
-- bandeja del panel. Opcional, informativo.

ALTER TABLE sofia_conversations
  ADD COLUMN IF NOT EXISTS bot_activo boolean NOT NULL DEFAULT true;

ALTER TABLE sofia_conversations
  ADD COLUMN IF NOT EXISTS atendido_por text NOT NULL DEFAULT 'bot';

COMMENT ON COLUMN sofia_conversations.bot_activo IS
  'Handoff: si FALSE, Sofía no responde (un humano atiende esa conversación).';
COMMENT ON COLUMN sofia_conversations.atendido_por IS
  'Último que respondió: bot | humano. Para la bandeja de agentes del panel.';
