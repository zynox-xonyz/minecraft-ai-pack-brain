package dev.packbrain;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.mojang.logging.LogUtils;
import com.mojang.serialization.JsonOps;

import net.minecraft.commands.CommandSourceStack;
import net.minecraft.core.Holder;
import net.minecraft.core.RegistryAccess;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.registries.Registries;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.RegistryOps;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.ai.attributes.Attribute;
import net.minecraft.world.entity.ai.attributes.AttributeSupplier;
import net.minecraft.world.entity.ai.attributes.DefaultAttributes;
import net.minecraft.world.food.FoodProperties;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.Recipe;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.component.ItemAttributeModifiers;
import net.minecraft.world.item.enchantment.Enchantment;
import net.minecraft.core.registries.Registries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.util.RandomSource;
import net.minecraft.world.effect.MobEffect;
import net.minecraft.world.effect.MobEffectInstance;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.MobCategory;
import net.minecraft.world.entity.TamableAnimal;
import net.minecraft.world.entity.animal.Animal;
import net.minecraft.world.entity.npc.Villager;
import net.minecraft.world.entity.npc.VillagerProfession;
import net.minecraft.world.entity.npc.VillagerTrades;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.alchemy.Potion;
import net.minecraft.world.item.alchemy.PotionBrewing;
import net.minecraft.world.item.alchemy.PotionContents;
import net.minecraft.world.item.trading.MerchantOffer;
import net.neoforged.fml.loading.FMLPaths;
import org.slf4j.Logger;

/**
 * /packbrain export  (op only): dumps what only the running game knows into config/../packbrain-export/ :
 *  items.json         every item with attribute modifiers (attack damage, armor, ...), durability, food, enchantability, rarity
 *  enchantments.json  every enchantment as the game loaded it (levels, weights, effects)
 *  recipes.json       every recipe as finally loaded (after all datapacks, without unmet load conditions)
 *  mobs.json          every entity type with its base attributes (health, attack damage, speed, armor ...) and size
 * The backend ingests these files; they make rankings like "strongest sword/armor" answerable.
 */
public final class ExportCommand {
    private static final Logger LOGGER = LogUtils.getLogger();
    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();

    private ExportCommand() {
    }

    private interface Task {
        int run() throws IOException;
    }

    private static int tryWrite(String name, Task task) {
        try {
            return task.run();
        } catch (IOException | RuntimeException e) {
            LOGGER.error("Pack-Brain export of {} failed", name, e);
            return -1;
        }
    }

    public static int run(CommandSourceStack source) {
        Path dir = FMLPaths.GAMEDIR.get().resolve("packbrain-export");
        try {
            Files.createDirectories(dir);
            RegistryAccess access = source.getServer().registryAccess();
            int items = tryWrite("items", () -> writeItems(dir.resolve("items.json")));
            int ench = tryWrite("enchantments", () -> writeEnchantments(dir.resolve("enchantments.json"), access));
            int recipes = tryWrite("recipes", () -> writeRecipes(dir.resolve("recipes.json"), source, access));
            int mobs = tryWrite("mobs", () -> writeMobs(dir.resolve("mobs.json")));
            ServerLevel overworld = source.getServer().overworld();
            int trades = tryWrite("trades", () -> writeTrades(dir.resolve("trades.json"), overworld));
            int animals = tryWrite("breeding", () -> writeBreeding(dir.resolve("breeding.json"), overworld));
            int mixes = tryWrite("brewing", () -> writeBrewing(dir.resolve("brewing.json"), source.getServer().potionBrewing()));
            int effects = tryWrite("effects", () -> writeEffects(dir.resolve("effects.json")));
            String msg = "[Pack-Brain] Exported " + items + " items, " + ench + " enchantments, " + recipes + " recipes, " + mobs
                    + " entity types, " + trades + " trades, " + animals + " animals, " + mixes + " brewing mixes, " + effects
                    + " effects to " + dir + " (-1 = that part failed, see latest.log)";
            source.sendSuccess(() -> Component.literal(msg), true);
            LOGGER.info(msg);
            return 1;
        } catch (IOException | RuntimeException e) {
            LOGGER.error("Export failed", e);
            source.sendFailure(Component.literal("[Pack-Brain] Export failed: " + e));
            return 0;
        }
    }

    private static int writeItems(Path file) throws IOException {
        JsonArray arr = new JsonArray();
        for (Item item : BuiltInRegistries.ITEM) {
            ResourceLocation key = BuiltInRegistries.ITEM.getKey(item);
            ItemStack st = item.getDefaultInstance();
            JsonObject o = new JsonObject();
            o.addProperty("id", key.toString());
            o.addProperty("max_stack", st.getMaxStackSize());
            if (st.getMaxDamage() > 0) {
                o.addProperty("durability", st.getMaxDamage());
            }
            o.addProperty("rarity", st.getRarity().name().toLowerCase());
            // swords/tools carry their modifiers in the stack component; armor provides them through the Item itself (1.21.1)
            java.util.List<ItemAttributeModifiers.Entry> entries = new java.util.ArrayList<>(
                    st.getOrDefault(DataComponents.ATTRIBUTE_MODIFIERS, ItemAttributeModifiers.EMPTY).modifiers());
            if (entries.isEmpty()) {
                entries.addAll(item.getDefaultAttributeModifiers().modifiers());
            }
            JsonArray ma = new JsonArray();
            for (ItemAttributeModifiers.Entry e : entries) {
                JsonObject m = new JsonObject();
                m.addProperty("attribute", e.attribute().unwrapKey().map(k -> k.location().toString()).orElse("?"));
                m.addProperty("amount", e.modifier().amount());
                m.addProperty("operation", e.modifier().operation().name().toLowerCase());
                m.addProperty("slot", e.slot().getSerializedName());
                ma.add(m);
            }
            if (ma.size() > 0) {
                o.add("attributes", ma);
            }
            FoodProperties food = st.get(DataComponents.FOOD);
            if (food != null) {
                o.addProperty("food_nutrition", food.nutrition());
                o.addProperty("food_saturation", food.saturation());
            }
            if (item.getEnchantmentValue() > 0) {
                o.addProperty("enchantability", item.getEnchantmentValue());
            }
            if (st.has(DataComponents.FIRE_RESISTANT)) {
                o.addProperty("fire_resistant", true);
            }
            arr.add(o);
        }
        Files.writeString(file, GSON.toJson(arr), StandardCharsets.UTF_8);
        return arr.size();
    }

    private static int writeEnchantments(Path file, RegistryAccess access) throws IOException {
        JsonObject out = new JsonObject();
        RegistryOps<JsonElement> ops = RegistryOps.create(JsonOps.INSTANCE, access);
        for (Holder.Reference<Enchantment> h : access.registryOrThrow(Registries.ENCHANTMENT).holders().toList()) {
            Enchantment.DIRECT_CODEC.encodeStart(ops, h.value())
                    .resultOrPartial(err -> LOGGER.warn("enchantment {}: {}", h.key().location(), err))
                    .ifPresent(json -> out.add(h.key().location().toString(), json));
        }
        Files.writeString(file, GSON.toJson(out), StandardCharsets.UTF_8);
        return out.size();
    }

    private static int writeRecipes(Path file, CommandSourceStack source, RegistryAccess access) throws IOException {
        JsonObject out = new JsonObject();
        int skipped = 0;
        RegistryOps<JsonElement> ops = RegistryOps.create(JsonOps.INSTANCE, access);
        for (RecipeHolder<?> holder : source.getServer().getRecipeManager().getRecipes()) {
            try {
                Recipe.CODEC.encodeStart(ops, holder.value())
                        .resultOrPartial(err -> LOGGER.debug("recipe {}: {}", holder.id(), err))
                        .ifPresent(json -> out.add(holder.id().toString(), json));
            } catch (RuntimeException e) {
                skipped++;  // e.g. TACZ gun smith table recipes throw UnsupportedOperationException when encoded
            }
        }
        if (skipped > 0) {
            LOGGER.info("Pack-Brain: {} recipes could not be encoded and were skipped", skipped);
        }
        Files.writeString(file, GSON.toJson(out), StandardCharsets.UTF_8);
        return out.size();
    }

    private static JsonObject stackJson(ItemStack st) {
        JsonObject o = new JsonObject();
        o.addProperty("item", BuiltInRegistries.ITEM.getKey(st.getItem()).toString());
        o.addProperty("count", st.getCount());
        return o;
    }

    /** Villager and wandering trader offers, sampled a few times per listing (prices/enchantments can be random). */
    private static int writeTrades(Path file, ServerLevel level) throws IOException {
        JsonArray arr = new JsonArray();
        RandomSource random = RandomSource.create(42);
        Villager villager = new Villager(EntityType.VILLAGER, level);
        for (Map.Entry<VillagerProfession, it.unimi.dsi.fastutil.ints.Int2ObjectMap<VillagerTrades.ItemListing[]>> e : VillagerTrades.TRADES.entrySet()) {
            ResourceLocation prof = BuiltInRegistries.VILLAGER_PROFESSION.getKey(e.getKey());
            villager.setVillagerData(villager.getVillagerData().setProfession(e.getKey()));
            for (var lvl : e.getValue().int2ObjectEntrySet()) {
                for (VillagerTrades.ItemListing listing : lvl.getValue()) {
                    addOffers(arr, prof.toString(), lvl.getIntKey(), listing, villager, random);
                }
            }
        }
        for (var lvl : VillagerTrades.WANDERING_TRADER_TRADES.int2ObjectEntrySet()) {
            for (VillagerTrades.ItemListing listing : lvl.getValue()) {
                addOffers(arr, "wandering_trader", lvl.getIntKey(), listing, villager, random);
            }
        }
        villager.discard();
        Files.writeString(file, GSON.toJson(arr), StandardCharsets.UTF_8);
        return arr.size();
    }

    private static void addOffers(JsonArray arr, String prof, int level, VillagerTrades.ItemListing listing, Entity trader, RandomSource random) {
        if (listing.getClass().getSimpleName().contains("Map")) {
            return;   // treasure maps search the world for structures: far too expensive for an export
        }
        java.util.Set<String> seen = new java.util.HashSet<>();
        for (int i = 0; i < 3; i++) {
            try {
                MerchantOffer offer = listing.getOffer(trader, random);
                if (offer == null || offer.getResult().isEmpty()) {
                    continue;
                }
                JsonObject o = new JsonObject();
                o.addProperty("profession", prof);
                o.addProperty("level", level);
                o.add("sells", stackJson(offer.getResult()));
                JsonArray buy = new JsonArray();
                buy.add(stackJson(offer.getCostA()));
                if (!offer.getCostB().isEmpty()) {
                    buy.add(stackJson(offer.getCostB()));
                }
                o.add("buy", buy);
                o.addProperty("max_uses", offer.getMaxUses());
                o.addProperty("xp", offer.getXp());
                if (seen.add(o.toString())) {
                    arr.add(o);
                }
            } catch (RuntimeException ex) {
                return;   // some modded listings need a real world context
            }
        }
    }

    /** Which items every animal accepts as food (breeding/feeding), and whether it is tamable. */
    private static int writeBreeding(Path file, ServerLevel level) throws IOException {
        JsonArray arr = new JsonArray();
        java.util.List<Item> items = BuiltInRegistries.ITEM.stream().toList();
        for (EntityType<?> type : BuiltInRegistries.ENTITY_TYPE) {
            MobCategory cat = type.getCategory();
            if (cat == MobCategory.MONSTER || cat == MobCategory.MISC) {
                continue;   // only creatures; constructing bosses or projectiles is pointless
            }
            Entity e;
            try {
                e = type.create(level);
            } catch (RuntimeException ex) {
                continue;
            }
            if (!(e instanceof Animal animal)) {
                if (e != null) {
                    e.discard();
                }
                continue;
            }
            JsonArray foods = new JsonArray();
            for (Item item : items) {
                try {
                    if (animal.isFood(new ItemStack(item))) {
                        foods.add(BuiltInRegistries.ITEM.getKey(item).toString());
                    }
                } catch (RuntimeException ex) {
                    // ignore single items
                }
            }
            JsonObject o = new JsonObject();
            o.addProperty("entity", BuiltInRegistries.ENTITY_TYPE.getKey(type).toString());
            o.addProperty("tamable", animal instanceof TamableAnimal);
            o.add("foods", foods);
            arr.add(o);
            animal.discard();
        }
        Files.writeString(file, GSON.toJson(arr), StandardCharsets.UTF_8);
        return arr.size();
    }

    /** All brewing stand mixes (potion + ingredient -> potion) and the effects of every potion. */
    private static int writeBrewing(Path file, PotionBrewing brewing) throws IOException {
        JsonArray mixes = new JsonArray();
        JsonArray potions = new JsonArray();
        java.util.List<Item> ingredients = BuiltInRegistries.ITEM.stream().filter(i -> {
            try {
                return brewing.isIngredient(new ItemStack(i));
            } catch (RuntimeException ex) {
                return false;
            }
        }).toList();
        for (Holder.Reference<Potion> p : BuiltInRegistries.POTION.holders().toList()) {
            JsonObject po = new JsonObject();
            po.addProperty("id", p.key().location().toString());
            JsonArray effs = new JsonArray();
            for (MobEffectInstance inst : p.value().getEffects()) {
                JsonObject eo = new JsonObject();
                eo.addProperty("effect", inst.getEffect().unwrapKey().map(k -> k.location().toString()).orElse("?"));
                eo.addProperty("duration", inst.getDuration());
                eo.addProperty("amplifier", inst.getAmplifier());
                effs.add(eo);
            }
            po.add("effects", effs);
            potions.add(po);
            ItemStack base = PotionContents.createItemStack(Items.POTION, p);
            for (Item ing : ingredients) {
                try {
                    ItemStack in = new ItemStack(ing);
                    if (!brewing.hasMix(base, in)) {
                        continue;
                    }
                    ItemStack out = brewing.mix(in, base.copy());
                    PotionContents pc = out.get(DataComponents.POTION_CONTENTS);
                    if (pc == null || pc.potion().isEmpty()) {
                        continue;
                    }
                    JsonObject m = new JsonObject();
                    m.addProperty("base", p.key().location().toString());
                    m.addProperty("ingredient", BuiltInRegistries.ITEM.getKey(ing).toString());
                    m.addProperty("result", pc.potion().get().unwrapKey().map(k -> k.location().toString()).orElse("?"));
                    m.addProperty("result_item", BuiltInRegistries.ITEM.getKey(out.getItem()).toString());
                    mixes.add(m);
                } catch (RuntimeException ex) {
                    // ignore single combinations
                }
            }
        }
        JsonObject out = new JsonObject();
        out.add("mixes", mixes);
        out.add("potions", potions);
        Files.writeString(file, GSON.toJson(out), StandardCharsets.UTF_8);
        return mixes.size();
    }

    private static int writeEffects(Path file) throws IOException {
        JsonArray arr = new JsonArray();
        for (Holder.Reference<MobEffect> h : BuiltInRegistries.MOB_EFFECT.holders().toList()) {
            JsonObject o = new JsonObject();
            o.addProperty("id", h.key().location().toString());
            o.addProperty("category", h.value().getCategory().name());
            arr.add(o);
        }
        Files.writeString(file, GSON.toJson(arr), StandardCharsets.UTF_8);
        return arr.size();
    }

    @SuppressWarnings("unchecked")
    private static int writeMobs(Path file) throws IOException {
        JsonArray arr = new JsonArray();
        for (EntityType<?> type : BuiltInRegistries.ENTITY_TYPE) {
            JsonObject o = new JsonObject();
            o.addProperty("id", BuiltInRegistries.ENTITY_TYPE.getKey(type).toString());
            o.addProperty("category", type.getCategory().getName());
            o.addProperty("width", type.getWidth());
            o.addProperty("height", type.getHeight());
            if (type.fireImmune()) {
                o.addProperty("fire_immune", true);
            }
            if (DefaultAttributes.hasSupplier(type)) {
                AttributeSupplier supplier = DefaultAttributes.getSupplier((EntityType<? extends LivingEntity>) type);
                JsonObject attrs = new JsonObject();
                for (Holder.Reference<Attribute> h : BuiltInRegistries.ATTRIBUTE.holders().toList()) {
                    if (supplier.hasAttribute(h)) {
                        attrs.addProperty(h.key().location().toString(), supplier.getBaseValue(h));
                    }
                }
                o.add("attributes", attrs);
            }
            arr.add(o);
        }
        Files.writeString(file, GSON.toJson(arr), StandardCharsets.UTF_8);
        return arr.size();
    }
}
