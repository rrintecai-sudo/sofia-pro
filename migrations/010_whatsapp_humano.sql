-- 010_whatsapp_humano.sql
-- Lista de "solo humano" por IDENTIFICADOR de contacto de WhatsApp.
--
-- Problema que resuelve: WhatsApp direcciona a los contactos con formatos
-- distintos — número normal ('5218441302112@s.whatsapp.net') o LID de privacidad
-- ('150...@lid'). El `session_id` de la conversación depende de ese formato, así
-- que marcar el handoff por `session_id` es frágil. Esta tabla marca por
-- IDENTIFICADOR estable, y el webhook revisa TODOS los identificadores del
-- contacto (dígitos del número + su @lid). Si cualquiera está aquí → Sofía NO
-- responde (lo atiende un humano).
--
-- Uso: se precargan aquí los contactos que un humano (Lily) ya atiende, para que
-- al cutover Sofía solo conteste a números NUEVOS.

CREATE TABLE IF NOT EXISTS whatsapp_humano (
  identificador text PRIMARY KEY,   -- dígitos del número, o '<lid>@lid'
  motivo text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE whatsapp_humano IS
  'Contactos de WhatsApp que atiende un humano (Sofía no responde). Clave: número (dígitos) o <lid>@lid.';
