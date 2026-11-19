package dev.packbrain;

import dev.packbrain.net.AnswerChunk;
import dev.packbrain.net.AskRequest;
import dev.packbrain.net.ExplainRequest;
import dev.packbrain.net.LocateRequest;
import net.minecraft.server.level.ServerPlayer;
import net.neoforged.neoforge.network.handling.IPayloadContext;

/** Payload handlers. Kept free of client-only types so the dedicated server can load this class. */
public final class ServerHandlers {
    private ServerHandlers() {
    }

    /** Runs on the server thread (NeoForge default for play payloads). */
    public static void onExplain(ExplainRequest req, IPayloadContext ctx) {
        if (ctx.player() instanceof ServerPlayer player) {
            AskService.explain(player, req.requestId(), req.itemId());
        }
    }

    /** Question typed into the Pack-Brain window. */
    public static void onAsk(AskRequest req, IPayloadContext ctx) {
        if (ctx.player() instanceof ServerPlayer player) {
            AskService.askFromScreen(player, req.requestId(), req.question());
        }
    }

    /** Location hotkey. */
    public static void onLocate(LocateRequest req, IPayloadContext ctx) {
        if (ctx.player() instanceof ServerPlayer player) {
            AskService.locate(player, req.requestId());
        }
    }

    /** Runs on the client; the real work lives in the client-only class, which is only touched when this executes. */
    public static void onAnswer(AnswerChunk chunk, IPayloadContext ctx) {
        ctx.enqueueWork(() -> dev.packbrain.client.ClientOverlay.accept(chunk));
    }
}
