"use strict";

// ARGOS — service worker. Só cuida das notificações push do painel: recebe
// o aviso enviado pelo bot (argos/webpush.py) e o mostra, mesmo com o painel
// fechado. Não faz cache de nada.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (ev) => ev.waitUntil(self.clients.claim()));

self.addEventListener("push", (ev) => {
  let d = {};
  try { d = ev.data ? ev.data.json() : {}; } catch { d = { mensagem: ev.data ? ev.data.text() : "" }; }
  ev.waitUntil(self.registration.showNotification(d.titulo || "ARGOS", {
    body: d.mensagem || "",
    icon: "/icones/icone-192.png",
    data: { url: d.url || "/" },
    // Prioridade máxima (vence em ≤ 30 min / vencida) fica na tela até a pessoa tocar.
    requireInteraction: (d.prioridade || 3) >= 5,
    vibrate: [200, 100, 200],
  }));
});

// Tocar na notificação: volta para o painel se já estiver aberto, senão abre.
self.addEventListener("notificationclick", (ev) => {
  ev.notification.close();
  ev.waitUntil((async () => {
    const janelas = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const j of janelas) {
      if ("focus" in j) return j.focus();
    }
    return self.clients.openWindow(ev.notification.data?.url || "/");
  })());
});
