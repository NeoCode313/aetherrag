"use strict";

window.addEventListener("DOMContentLoaded", () => {
  SwaggerUIBundle({
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    presets: [SwaggerUIBundle.presets.apis],
    layout: "BaseLayout",
    validatorUrl: null,
    persistAuthorization: false,
    requestInterceptor: async (request) => {
      const target = new URL(request.url, window.location.origin);
      if (target.origin !== window.location.origin) throw new Error("La documentación solo consulta esta API.");
      request.credentials = "same-origin";
      if (!["GET", "HEAD", "OPTIONS"].includes(String(request.method || "GET").toUpperCase())
          && !["/api/v1/auth/login", "/api/v1/auth/register"].includes(target.pathname)) {
        const response = await fetch("/api/v1/auth/session", { credentials: "same-origin" });
        if (!response.ok) throw new Error("Inicia sesión en la aplicación o usa /api/v1/auth/login antes de modificar datos.");
        const session = await response.json();
        if (session.csrf_token) request.headers["X-CSRF-Token"] = session.csrf_token;
      }
      return request;
    },
  });
});
