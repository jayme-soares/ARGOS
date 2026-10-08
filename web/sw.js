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
    // Mesma tag substitui o aviso anterior (ex.: "N avisos sem confirmar"),
    // mas toca/vibra de novo (renotify).
    ...(d.tag ? { tag: d.tag, renotify: true } : {}),
    // Prioridade máxima (vence em ≤ 30 min / vencida) fica na tela até a pessoa tocar.
    requireInteraction: (d.prioridade || 3) >= 5,
    vibrate: [200, 100, 200],
  }));
});

// Tocar na notificação: volta para o painel do aviso (gestão "/" ou equipe
// "/equipe") se já estiver aberto, senão leva uma janela aberta até ele ou
// abre uma nova.
self.addEventListener("notificationclick", (ev) => {
  ev.notification.close();
  ev.waitUntil((async () => {
    const destino = new URL(ev.notification.data?.url || "/", self.location.origin);
    const janelas = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const mesma = janelas.find((j) => new URL(j.url).pathname === destino.pathname);
    if (mesma && "focus" in mesma) return mesma.focus();
    const outra = janelas.find((j) => "navigate" in j);
    if (outra) {
      try { return (await outra.navigate(destino.href))?.focus(); } catch { /* janela não controlada */ }
    }
    return self.clients.openWindow(destino.href);
  })());
});
