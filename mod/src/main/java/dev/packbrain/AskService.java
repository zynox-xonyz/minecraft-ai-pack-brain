package dev.packbrain;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.mojang.logging.LogUtils;

import dev.packbrain.net.AnswerChunk;
import net.minecraft.ChatFormatting;
import net.minecraft.advancements.AdvancementHolder;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.ClickEvent;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.HoverEvent;
import net.minecraft.network.chat.MutableComponent;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.PlayerAdvancements;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.core.registries.Registries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.Container;
import net.minecraft.world.entity.Mob;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.levelgen.structure.Structure;
import net.neoforged.neoforge.capabilities.Capabilities;
import net.neoforged.neoforge.items.IItemHandler;
import net.neoforged.neoforge.network.PacketDistributor;
import org.slf4j.Logger;

/** Server-side orchestration: backend requests run on worker threads, results are delivered on the server thread. */
public final class AskService {
    private static final Logger LOGGER = LogUtils.getLogger();
    private static final ExecutorService POOL = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "packbrain-http");
        t.setDaemon(true);
        return t;
    });
    private static final Pattern ITEM_ID = Pattern.compile("^[a-z0-9_.\\-]+:[a-z0-9_./\\-]+$");
    /** Questions that need the contents of nearby containers ("was fehlt mir für X", project status). */
    private static final Pattern NEEDS_STOCK = Pattern.compile(
            "(?i)(fehlt|fehlen|genug|reicht|missing|enough|projekt|project)");
    private static final Map<String, String> LAST_ANSWER = new ConcurrentHashMap<>();

    private AskService() {
    }

    private static BackendClient client() {
        return new BackendClient(PBConfig.BACKEND_URL.get(), PBConfig.API_TOKEN.get(), PBConfig.TIMEOUT_SECONDS.get());
    }

    // ------------------------------------------------------------------ player context (read on the server thread)
    private static String id(ItemStack s) {
        return BuiltInRegistries.ITEM.getKey(s.getItem()).toString();
    }

    /** What the game knows about the asking player right now. Sent with every question; enables "what should I do next". */
    static JsonObject playerContext(ServerPlayer p) {
        JsonObject c = new JsonObject();
        try {
            BlockPos pos = p.blockPosition();
            c.addProperty("dimension", p.level().dimension().location().toString());
            p.level().getBiome(pos).unwrapKey().ifPresent(k -> c.addProperty("biome", k.location().toString()));
            c.addProperty("x", pos.getX());
            c.addProperty("y", pos.getY());
            c.addProperty("z", pos.getZ());
            if (!p.getMainHandItem().isEmpty()) {
                c.addProperty("held_item", id(p.getMainHandItem()));
            }
            JsonArray armor = new JsonArray();
            for (ItemStack s : p.getArmorSlots()) {
                if (!s.isEmpty()) {
                    armor.add(id(s));
                }
            }
            if (!armor.isEmpty()) {
                c.add("armor", armor);
            }
            Map<String, Integer> inv = new LinkedHashMap<>();
            for (ItemStack s : p.getInventory().items) {
                if (!s.isEmpty()) {
                    inv.merge(id(s), s.getCount(), Integer::sum);
                }
            }
            JsonObject invJson = new JsonObject();
            inv.entrySet().stream().sorted((a, b) -> b.getValue() - a.getValue()).limit(25)
                    .forEach(e -> invJson.addProperty(e.getKey(), e.getValue()));
            c.add("inventory", invJson);
            c.addProperty("health", Math.round(p.getHealth()));
            c.addProperty("food", p.getFoodData().getFoodLevel());
            c.addProperty("gamemode", p.gameMode.getGameModeForPlayer().getName());
            c.addProperty("time", p.level().isDay() ? "day" : "night");
            PlayerAdvancements adv = p.getAdvancements();
            JsonArray done = new JsonArray();
            int count = 0;
            for (AdvancementHolder h : p.getServer().getAdvancements().getAllAdvancements()) {
                if (h.value().display().isPresent() && adv.getOrStartProgress(h).isDone()) {
                    count++;
                    if (done.size() < 60) {
                        done.add(h.id().toString());
                    }
                }
            }
            c.addProperty("advancements_done", count);
            c.add("advancements", done);
        } catch (RuntimeException e) {
            LOGGER.debug("Pack-Brain: could not read player context: {}", e.toString());
        }
        return c;
    }

    /** Item counts in containers around the player (vanilla Container or any NeoForge item handler, e.g. modded storage). */
    static JsonObject nearbyContainers(ServerPlayer p) {
        JsonObject out = new JsonObject();
        int r = PBConfig.NEARBY_RADIUS.get();
        if (r <= 0) {
            return out;
        }
        Map<String, Integer> counts = new LinkedHashMap<>();
        ServerLevel level = p.serverLevel();
        BlockPos c = p.blockPosition();
        try {
            for (BlockPos pos : BlockPos.betweenClosed(c.offset(-r, -r, -r), c.offset(r, r, r))) {
                BlockEntity be = level.getBlockEntity(pos);
                if (be == null) {
                    continue;
                }
                IItemHandler h = level.getCapability(Capabilities.ItemHandler.BLOCK, pos, null);
                if (h != null) {
                    for (int i = 0; i < h.getSlots(); i++) {
                        ItemStack s = h.getStackInSlot(i);
                        if (!s.isEmpty()) {
                            counts.merge(id(s), s.getCount(), Integer::sum);
                        }
                    }
                } else if (be instanceof Container cont) {
                    for (int i = 0; i < cont.getContainerSize(); i++) {
                        ItemStack s = cont.getItem(i);
                        if (!s.isEmpty()) {
                            counts.merge(id(s), s.getCount(), Integer::sum);
                        }
                    }
                }
            }
        } catch (RuntimeException e) {
            LOGGER.debug("Pack-Brain: could not read nearby containers: {}", e.toString());
        }
        counts.entrySet().stream().sorted((a, b) -> b.getValue() - a.getValue()).limit(300)
                .forEach(e -> out.addProperty(e.getKey(), e.getValue()));
        return out;
    }

    private static JsonObject contextFor(ServerPlayer p, String question) {
        JsonObject c = playerContext(p);
        if (NEEDS_STOCK.matcher(question).find()) {
            c.add("nearby", nearbyContainers(p));
        }
        return c;
    }

    /** /share <player>: send your last Pack-Brain answer to another player. */
    public static int share(CommandSourceStack source, ServerPlayer target) {
        String name = source.getEntity() instanceof ServerPlayer p ? p.getGameProfile().getName() : "Console";
        String last = LAST_ANSWER.get(name);
        if (last == null) {
            source.sendFailure(Component.literal("[Pack-Brain] Du hast noch keine Antwort zum Teilen."));
            return 0;
        }
        target.sendSystemMessage(Component.literal("[Pack-Brain, von " + name + "] ").withStyle(ChatFormatting.AQUA)
                .append(Component.literal(last).withStyle(ChatFormatting.WHITE)));
        source.sendSuccess(() -> Component.literal("[Pack-Brain] Antwort an " + target.getGameProfile().getName() + " geschickt."), false);
        return 1;
    }

    // ------------------------------------------------------------------ hotkey "what is this place"
    public static void locate(ServerPlayer player, int requestId) {
        JsonObject loc = new JsonObject();
        try {
            ServerLevel level = player.serverLevel();
            BlockPos pos = player.blockPosition();
            loc.addProperty("dimension", level.dimension().location().toString());
            level.getBiome(pos).unwrapKey().ifPresent(k -> loc.addProperty("biome", k.location().toString()));
            loc.addProperty("x", pos.getX());
            loc.addProperty("y", pos.getY());
            loc.addProperty("z", pos.getZ());
            loc.addProperty("light", level.getMaxLocalRawBrightness(pos));
            loc.addProperty("day", level.isDay());
            loc.addProperty("moon", level.getMoonPhase());
            JsonArray inside = new JsonArray();
            JsonArray near = new JsonArray();
            var registry = level.registryAccess().registryOrThrow(Registries.STRUCTURE);
            for (Structure st : level.structureManager().getAllStructuresAt(pos).keySet()) {
                ResourceLocation key = registry.getKey(st);
                if (key == null) {
                    continue;
                }
                if (level.structureManager().getStructureWithPieceAt(pos, st).isValid()) {
                    inside.add(key.toString());
                } else {
                    near.add(key.toString());
                }
            }
            loc.add("structures", inside);
            loc.add("structures_near", near);
            Map<String, Integer> mobs = new LinkedHashMap<>();
            for (Mob m : level.getEntitiesOfClass(Mob.class, player.getBoundingBox().inflate(24))) {
                mobs.merge(BuiltInRegistries.ENTITY_TYPE.getKey(m.getType()).toString(), 1, Integer::sum);
            }
            JsonObject mj = new JsonObject();
            mobs.entrySet().stream().sorted((a, b) -> b.getValue() - a.getValue()).limit(12).forEach(e -> mj.addProperty(e.getKey(), e.getValue()));
            loc.add("mobs_near", mj);
        } catch (RuntimeException e) {
            LOGGER.debug("Pack-Brain: could not read the location: {}", e.toString());
        }
        JsonObject body = new JsonObject();
        body.addProperty("player", player.getGameProfile().getName());
        body.add("location", loc);
        body.addProperty("brevity", "overlay");
        body.addProperty("lang", "de");
        body.addProperty("game_lang", gameLang(player));
        stream(player, requestId, "/v1/where", body);
    }

    // ------------------------------------------------------------------ /ask <question>  (answer in chat, item names clickable)
    public static void ask(CommandSourceStack source, String question, boolean newConversation) {
        MinecraftServer server = source.getServer();
        String player = source.getEntity() instanceof ServerPlayer p ? p.getGameProfile().getName() : "Console";
        BackendClient client = client();
        int maxLines = PBConfig.MAX_CHAT_LINES.get();
        JsonObject body = new JsonObject();
        body.addProperty("player", player);
        body.addProperty("question", question);
        body.addProperty("brevity", "chat");
        body.addProperty("new_conversation", newConversation);
        if (source.getEntity() instanceof ServerPlayer p) {
            body.add("context", contextFor(p, question));
            body.addProperty("game_lang", gameLang(p));
            p.displayClientMessage(Component.translatable("packbrain.thinking"), true);
        }
        POOL.execute(() -> {
            String[] answer = {null};
            String[] error = {null};
            JsonArray[] items = {null};
            try {
                client.stream("/v1/ask", body, new BackendClient.Listener() {
                    @Override
                    public void onEvent(String type, JsonObject d) {
                        if (type.equals("done") && d.has("answer")) {
                            answer[0] = d.get("answer").getAsString();
                            items[0] = d.has("items") && d.get("items").isJsonArray() ? d.getAsJsonArray("items") : null;
                        } else if (type.equals("error")) {
                            error[0] = d.has("message") ? d.get("message").getAsString() : "error";
                        }
                    }

                    @Override
                    public void onHttpError(int status, String message, int retry) {
                        error[0] = httpMessage(status, message, retry);
                    }
                });
            } catch (Exception e) {
                LOGGER.warn("Pack-Brain backend call failed: {}", e.toString());
                error[0] = "The Pack-Brain backend is not reachable.";
            }
            server.execute(() -> {
                if (source.getEntity() instanceof ServerPlayer p) {
                    p.displayClientMessage(Component.empty(), true);
                }
                if (error[0] != null) {
                    source.sendFailure(Component.literal("[Pack-Brain] " + error[0]));
                } else if (answer[0] != null) {
                    LAST_ANSWER.put(player, answer[0]);
                    sendChat(source, answer[0], maxLines, items[0]);
                }
            });
        });
    }

    private static void sendChat(CommandSourceStack source, String text, int maxLines, JsonArray items) {
        List<String> lines = new ArrayList<>();
        for (String l : text.split("\n")) {
            if (!l.isBlank()) {
                lines.add(l.strip());
            }
        }
        boolean cut = lines.size() > maxLines;
        for (int i = 0; i < Math.min(lines.size(), maxLines); i++) {
            Component prefix = i == 0 ? Component.literal("[Pack-Brain] ").withStyle(ChatFormatting.AQUA) : Component.literal("  ");
            String line = lines.get(i);
            source.sendSuccess(() -> Component.empty().append(prefix).append(Component.literal(line).withStyle(ChatFormatting.WHITE)), false);
        }
        if (cut) {
            source.sendSuccess(() -> Component.literal("  ...").withStyle(ChatFormatting.GRAY), false);
        }
        if (items != null && !items.isEmpty()) {   // clickable item names: open the recipe in JEI (needs the mod on the client)
            MutableComponent row = Component.literal("  JEI: ").withStyle(ChatFormatting.GRAY);
            for (JsonElement e : items) {
                JsonObject o = e.getAsJsonObject();
                String iid = o.get("id").getAsString();
                if (!ITEM_ID.matcher(iid).matches()) {
                    continue;
                }
                row.append(Component.literal("[" + o.get("name").getAsString() + "] ").withStyle(st -> st.withColor(ChatFormatting.AQUA)
                        .withClickEvent(new ClickEvent(ClickEvent.Action.RUN_COMMAND, "/packbrain_client jei " + iid))
                        .withHoverEvent(new HoverEvent(HoverEvent.Action.SHOW_TEXT, Component.literal("Rezept in JEI anzeigen")))));
            }
            source.sendSuccess(() -> row, false);
        }
    }

    // ------------------------------------------------------------------ question window (hotkey K): stream the answer to the client
    public static void askFromScreen(ServerPlayer player, int requestId, String question) {
        String q = question == null ? "" : question.strip();
        if (q.isEmpty() || q.length() > 500) {
            return;
        }
        JsonObject body = new JsonObject();
        body.addProperty("player", player.getGameProfile().getName());
        body.addProperty("question", q);
        body.addProperty("brevity", "overlay");
        body.add("context", contextFor(player, q));
        body.addProperty("game_lang", gameLang(player));
        stream(player, requestId, "/v1/ask", body);
    }

    // ------------------------------------------------------------------ hotkey H: stream an item explanation to the client
    public static void explain(ServerPlayer player, int requestId, String itemId) {
        if (!ITEM_ID.matcher(itemId).matches()) {
            return;
        }
        JsonObject body = new JsonObject();
        body.addProperty("player", player.getGameProfile().getName());
        body.addProperty("item_id", itemId);
        body.addProperty("brevity", "overlay");
        body.addProperty("lang", "de");
        body.addProperty("game_lang", gameLang(player));
        stream(player, requestId, "/v1/explain", body);
    }

    /** Client language (e.g. de_de): answers then use item names exactly as this player sees them in the game. */
    private static String gameLang(ServerPlayer player) {
        try {
            String l = player.clientInformation().language();
            return l == null || l.length() > 12 ? "en_us" : l.toLowerCase(java.util.Locale.ROOT);
        } catch (RuntimeException e) {
            return "en_us";
        }
    }

    private static void stream(ServerPlayer player, int requestId, String path, JsonObject body) {
        MinecraftServer server = player.getServer();
        BackendClient client = client();
        POOL.execute(() -> {
            try {
                client.stream(path, body, new BackendClient.Listener() {
                    @Override
                    public void onEvent(String type, JsonObject d) {
                        switch (type) {
                            case "queued" -> send(server, player, requestId, AnswerChunk.STATUS, "waiting for the model ...");
                            case "tool" -> send(server, player, requestId, AnswerChunk.STATUS, "searching: " + str(d, "name"));
                            case "token" -> send(server, player, requestId, AnswerChunk.TOKEN, str(d, "text"));
                            case "reset" -> send(server, player, requestId, AnswerChunk.RESET, "");
                            case "done" -> {
                                if (d.has("items") && d.get("items").isJsonArray() && !d.getAsJsonArray("items").isEmpty()) {
                                    send(server, player, requestId, AnswerChunk.ITEMS, d.getAsJsonArray("items").toString());
                                }
                                LAST_ANSWER.put(player.getGameProfile().getName(), str(d, "answer"));
                                send(server, player, requestId, AnswerChunk.DONE, str(d, "answer"));
                            }
                            case "error" -> send(server, player, requestId, AnswerChunk.ERROR, str(d, "message"));
                            default -> {
                            }
                        }
                    }

                    @Override
                    public void onHttpError(int status, String message, int retry) {
                        send(server, player, requestId, AnswerChunk.ERROR, httpMessage(status, message, retry));
                    }
                });
            } catch (Exception e) {
                LOGGER.warn("Pack-Brain backend call failed: {}", e.toString());
                send(server, player, requestId, AnswerChunk.ERROR, "The Pack-Brain backend is not reachable.");
            }
        });
    }

    private static void send(MinecraftServer server, ServerPlayer player, int requestId, int kind, String text) {
        server.execute(() -> {
            if (player.connection != null && player.connection.hasChannel(AnswerChunk.TYPE)) {
                PacketDistributor.sendToPlayer(player, new AnswerChunk(requestId, kind, text));
            }
        });
    }

    private static String str(JsonObject d, String key) {
        return d.has(key) && !d.get(key).isJsonNull() ? d.get(key).getAsString() : "";
    }

    static String httpMessage(int status, String message, int retry) {
        return switch (status) {
            case 401 -> "Backend rejected the API token (check packbrain-common.toml on the server).";
            case 429 -> "Please wait a moment" + (retry > 0 ? " (" + retry + " s)" : "") + ": " + message;
            case 503 -> "The assistant is busy, try again in a moment.";
            case 404 -> "Not found: " + message;
            default -> "Backend error " + status + ": " + message;
        };
    }
}
