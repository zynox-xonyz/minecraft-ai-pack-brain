package dev.packbrain;

import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.logging.LogUtils;

import dev.packbrain.net.AnswerChunk;
import dev.packbrain.net.AskRequest;
import dev.packbrain.net.ExplainRequest;
import dev.packbrain.net.LocateRequest;
import net.minecraft.commands.arguments.EntityArgument;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.config.ModConfig;
import net.neoforged.neoforge.event.RegisterCommandsEvent;
import net.neoforged.neoforge.event.server.ServerStartedEvent;
import net.neoforged.neoforge.network.event.RegisterPayloadHandlersEvent;
import net.neoforged.neoforge.network.registration.PayloadRegistrar;
import org.slf4j.Logger;

@Mod(PackBrainMod.MODID)
public class PackBrainMod {
    public static final String MODID = "packbrain";
    private static final Logger LOGGER = LogUtils.getLogger();

    public PackBrainMod(IEventBus modBus, ModContainer container) {
        // COMMON config is not synced to clients: the API token stays on the server.
        container.registerConfig(ModConfig.Type.COMMON, PBConfig.SPEC);
        modBus.addListener(PackBrainMod::registerPayloads);
    }

    /** Optional payloads: clients without the mod can still join; they just have no hotkey. */
    private static void registerPayloads(RegisterPayloadHandlersEvent event) {
        PayloadRegistrar registrar = event.registrar("1").optional();
        registrar.playToServer(ExplainRequest.TYPE, ExplainRequest.CODEC, ServerHandlers::onExplain);
        registrar.playToServer(AskRequest.TYPE, AskRequest.CODEC, ServerHandlers::onAsk);
        registrar.playToServer(LocateRequest.TYPE, LocateRequest.CODEC, ServerHandlers::onLocate);
        registrar.playToClient(AnswerChunk.TYPE, AnswerChunk.CODEC, ServerHandlers::onAnswer);
    }

    @EventBusSubscriber(modid = MODID)
    public static class Events {
        @SubscribeEvent
        public static void onCommands(RegisterCommandsEvent event) {
            event.getDispatcher().register(Commands.literal("ask")
                    .then(Commands.argument("question", StringArgumentType.greedyString())
                            .executes(ctx -> {
                                AskService.ask(ctx.getSource(), StringArgumentType.getString(ctx, "question"), false);
                                return 1;
                            })));
            event.getDispatcher().register(Commands.literal("share")
                    .then(Commands.argument("player", EntityArgument.player())
                            .executes(ctx -> AskService.share(ctx.getSource(), EntityArgument.getPlayer(ctx, "player")))));
            event.getDispatcher().register(Commands.literal("askreset").executes(ctx -> {
                AskService.ask(ctx.getSource(), "Vergiss unser bisheriges Gespräch.", true);
                return 1;
            }));
            event.getDispatcher().register(Commands.literal("packbrain")
                    .requires(s -> s.hasPermission(3))
                    .then(Commands.literal("export").executes(ctx -> ExportCommand.run(ctx.getSource())))
                    .then(Commands.literal("status").executes(ctx -> status(ctx.getSource()))));
        }

        @SubscribeEvent
        public static void onServerStarted(ServerStartedEvent event) {
            if (PBConfig.API_TOKEN.get().isBlank()) {
                LOGGER.warn("Pack-Brain: apiToken is empty in config/packbrain-common.toml - /ask will be rejected by the backend.");
            }
        }

        private static int status(CommandSourceStack source) {
            BackendClient c = new BackendClient(PBConfig.BACKEND_URL.get(), PBConfig.API_TOKEN.get(), 5);
            String url = PBConfig.BACKEND_URL.get();
            new Thread(() -> {
                boolean ok = c.healthy();
                source.getServer().execute(() -> source.sendSuccess(
                        () -> Component.literal("[Pack-Brain] backend " + url + (ok ? " is reachable" : " is NOT reachable")), false));
            }, "packbrain-status").start();
            return 1;
        }
    }
}
