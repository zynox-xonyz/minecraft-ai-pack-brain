package dev.packbrain.net;

import dev.packbrain.PackBrainMod;
import io.netty.buffer.ByteBuf;
import net.minecraft.network.codec.ByteBufCodecs;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.ResourceLocation;

/** Client -> server: "explain this item" (hotkey). */
public record ExplainRequest(int requestId, String itemId) implements CustomPacketPayload {
    public static final Type<ExplainRequest> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(PackBrainMod.MODID, "explain_request"));
    public static final StreamCodec<ByteBuf, ExplainRequest> CODEC = StreamCodec.composite(
            ByteBufCodecs.VAR_INT, ExplainRequest::requestId,
            ByteBufCodecs.STRING_UTF8, ExplainRequest::itemId,
            ExplainRequest::new);

    @Override
    public Type<? extends CustomPacketPayload> type() {
        return TYPE;
    }
}
