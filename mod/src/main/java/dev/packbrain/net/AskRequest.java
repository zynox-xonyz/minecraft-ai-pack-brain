package dev.packbrain.net;

import dev.packbrain.PackBrainMod;
import io.netty.buffer.ByteBuf;
import net.minecraft.network.codec.ByteBufCodecs;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.ResourceLocation;

/** Client -> server: a free question typed into the Pack-Brain window (hotkey K). */
public record AskRequest(int requestId, String question) implements CustomPacketPayload {
    public static final Type<AskRequest> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(PackBrainMod.MODID, "ask_request"));
    public static final StreamCodec<ByteBuf, AskRequest> CODEC = StreamCodec.composite(
            ByteBufCodecs.VAR_INT, AskRequest::requestId,
            ByteBufCodecs.stringUtf8(600), AskRequest::question,
            AskRequest::new);

    @Override
    public Type<? extends CustomPacketPayload> type() {
        return TYPE;
    }
}
