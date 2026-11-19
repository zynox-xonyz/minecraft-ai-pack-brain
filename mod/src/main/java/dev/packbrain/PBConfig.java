package dev.packbrain;

import net.neoforged.neoforge.common.ModConfigSpec;

/**
 * COMMON config (config/packbrain-common.toml). COMMON configs are NOT synced to clients, so the API token stays on the
 * machine that reads it (the dedicated server). Do not put the token into a client-side config.
 */
public final class PBConfig {
    private static final ModConfigSpec.Builder B = new ModConfigSpec.Builder();

    public static final ModConfigSpec.ConfigValue<String> BACKEND_URL = B
            .comment("Base URL of the Pack-Brain backend service (python -m packbrain).")
            .define("backendUrl", "http://127.0.0.1:8787");

    public static final ModConfigSpec.ConfigValue<String> API_TOKEN = B
            .comment("API token from the backend's config.json. Keep this file on the server only.")
            .define("apiToken", "");

    public static final ModConfigSpec.IntValue TIMEOUT_SECONDS = B
            .comment("Maximum time to wait for one answer.")
            .defineInRange("timeoutSeconds", 90, 10, 600);

    public static final ModConfigSpec.IntValue MAX_CHAT_LINES = B
            .comment("Maximum number of chat lines used for one /ask answer.")
            .defineInRange("maxChatLines", 14, 3, 40);

    public static final ModConfigSpec.IntValue NEARBY_RADIUS = B
            .comment("Radius in blocks around the player whose containers are counted for 'what am I missing' / project questions (0 = off).")
            .defineInRange("nearbyContainerRadius", 6, 0, 16);

    public static final ModConfigSpec SPEC = B.build();

    private PBConfig() {
    }
}
