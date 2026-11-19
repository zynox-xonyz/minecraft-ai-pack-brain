package dev.packbrain;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

/**
 * Minimal HTTP + Server-Sent-Events client for the Pack-Brain backend. No Minecraft classes in here, so it can be
 * tested on its own. All methods block: call them from a worker thread, never from the server thread.
 */
public final class BackendClient {
    /** Callbacks are invoked on the calling (worker) thread. */
    public interface Listener {
        void onEvent(String type, JsonObject data);

        /** HTTP status other than 200 before streaming started (401 bad token, 429 rate limit, 503 busy, ...). */
        void onHttpError(int status, String message, int retryAfterSeconds);
    }

    private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    private final String baseUrl;
    private final String token;
    private final int timeoutSeconds;

    public BackendClient(String baseUrl, String token, int timeoutSeconds) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.token = token;
        this.timeoutSeconds = timeoutSeconds;
    }

    public void stream(String path, JsonObject body, Listener listener) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(baseUrl + path))
                .timeout(Duration.ofSeconds(timeoutSeconds))
                .header("Authorization", "Bearer " + token)
                .header("Content-Type", "application/json")
                .header("Accept", "text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(body.toString(), StandardCharsets.UTF_8))
                .build();
        HttpResponse<InputStream> resp = HTTP.send(req, HttpResponse.BodyHandlers.ofInputStream());
        try (InputStream in = resp.body()) {
            if (resp.statusCode() != 200) {
                String text = new String(in.readAllBytes(), StandardCharsets.UTF_8);
                int retry = resp.headers().firstValue("Retry-After").map(v -> parseInt(v, 0)).orElse(0);
                listener.onHttpError(resp.statusCode(), detail(text), retry);
                return;
            }
            BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            String line, event = "message";
            StringBuilder data = new StringBuilder();
            while ((line = r.readLine()) != null) {
                if (line.startsWith("event:")) {
                    event = line.substring(6).trim();
                } else if (line.startsWith("data:")) {
                    data.append(line.substring(5).trim());
                } else if (line.isEmpty() && data.length() > 0) {
                    JsonObject obj;
                    try {
                        obj = JsonParser.parseString(data.toString()).getAsJsonObject();
                    } catch (RuntimeException e) {
                        obj = new JsonObject();
                    }
                    listener.onEvent(event, obj);
                    event = "message";
                    data.setLength(0);
                }
            }
        }
    }

    /** GET /v1/health -> true if the service answers ok. */
    public boolean healthy() {
        try {
            HttpRequest req = HttpRequest.newBuilder(URI.create(baseUrl + "/v1/health")).timeout(Duration.ofSeconds(4)).GET().build();
            return HTTP.send(req, HttpResponse.BodyHandlers.discarding()).statusCode() == 200;
        } catch (IOException e) {
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    private static String detail(String body) {
        try {
            JsonObject o = JsonParser.parseString(body).getAsJsonObject();
            if (o.has("detail")) {
                return o.get("detail").isJsonPrimitive() ? o.get("detail").getAsString() : o.get("detail").toString();
            }
        } catch (RuntimeException ignored) {
            // not JSON
        }
        return body.length() > 200 ? body.substring(0, 200) : body;
    }

    private static int parseInt(String v, int dflt) {
        try {
            return Integer.parseInt(v.trim());
        } catch (NumberFormatException e) {
            return dflt;
        }
    }
}
