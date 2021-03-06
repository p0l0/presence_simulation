"""Component to integrate with presence_simulation."""

import logging
import time
import asyncio
from datetime import datetime,timedelta,timezone
from homeassistant.components import history
from .const import (
        DOMAIN,
        SWITCH_PLATFORM,
        SWITCH
)
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry):
    """Set up this component using config flow."""
    _LOGGER.debug("async setup entry %s", entry.data["entities"])
    unsub = entry.add_update_listener(update_listener)

    # Use `hass.async_create_task` to avoid a circular dependency between the platform and the component
    hass.async_create_task(hass.config_entries.async_forward_entry_setup(entry, SWITCH_PLATFORM))
    if 'interval' in entry.data:
        interval = entry.data['interval']
    else:
        interval = 30
    return await async_mysetup(hass, [entry.data["entities"]], entry.data["delta"], interval)

async def async_setup(hass, config):
    """Set up this component using YAML."""
    if config.get(DOMAIN) is None:
        # We get here if the integration is set up using config flow
        return True
    return await async_mysetup(hass, config[DOMAIN].get("entity_id",[]), config[DOMAIN].get('delta', "7"), config[DOMAIN].get('interval', '30'))


async def async_mysetup(hass, entities, deltaStr, refreshInterval):
    """Set up this component (YAML or UI)."""
    #delta is the size in days of the historic to get from the DB
    delta = int(deltaStr)
    #delta = 1/24+4/24/6 # for test
    #interval is the number of seconds the component will wait before checking if the entity need to be switch
    interval = int(refreshInterval)
    _LOGGER.debug("Config: Entities for presence simulation: %s", entities)
    _LOGGER.debug("Config: Cycle of %s days", delta)
    _LOGGER.debug("Config: Scan interval of %s seconds", interval)
    _LOGGER.debug("Config: Timezone that will be used to display datetime: %s", hass.config.time_zone)

    async def stop_presence_simulation(err=None, restart=False):
        """Stop the presence simulation, raising a potential error"""
        #get the switch entity
        entity = hass.data[DOMAIN][SWITCH_PLATFORM][SWITCH]
        #set the state of the switch to off. Not calling turn_off to avoid calling the stop service again
        entity.internal_turn_off()
        if not restart:
            #empty the start_datetime  attribute
            await entity.reset_start_datetime()
            await entity.reset_entities()
        if err is not None:
            _LOGGER.debug("Error in presence simulation, exiting")
            raise e

    async def handle_stop_presence_simulation(call, restart=False):
        """Stop the presence simulation"""
        _LOGGER.debug("Stopped presence simulation")
        await stop_presence_simulation(restart=restart)

    async def async_expand_entities(entities):
        """If the entity is a group, return the list of the entities within, otherwise, return the entity"""
        entities_new = []
        for entity in entities:
            #to make it asyncable, not sure it is needed
            await asyncio.sleep(0)
            if entity[0:5] == "group": # if the domain of the entity is a group:
                try:
                    #get the list of the associated entities
                    group_entities = hass.states.get(entity).attributes["entity_id"]
                except Exception as e:
                    _LOGGER.error("Error when trying to identify entity %s: %s", entity, e)
                else:
                    #and call recursively, in case a group contains a group
                    group_entities_expanded = await async_expand_entities(group_entities)
                    _LOGGER.debug("State %s", group_entities_expanded)
                    for tmp in group_entities_expanded:
                        entities_new.append(tmp)
            else:
                try:
                    hass.states.get(entity)
                except Exception as e:
                    _LOGGER.error("Error when trying to identify entity %s: %s", entity, e)
                else:
                    entities_new.append(entity)
        return entities_new

    async def handle_presence_simulation(call, restart=False):
        """Start the presence simulation"""
        if call is not None: #if we are here, it is a call of the service, or a restart at the end of a cycle
            if isinstance(call.data.get("entity_id", entities), list):
                overridden_entities = call.data.get("entity_id", entities)
            else:
                overridden_entities = [call.data.get("entity_id", entities)]
            overridden_delta = call.data.get("delta", delta)
        elif not restart: #if we are it is a call from the toggle service or from the turn_on action of the switch entity
            overridden_entities = entities
            overridden_delta = delta
        #else, should not happen

        #get the switch entity
        entity = hass.data[DOMAIN][SWITCH_PLATFORM][SWITCH]
        _LOGGER.debug("Is already running ? %s", entity.state)
        if is_running():
            _LOGGER.warning("Presence simulation already running. Doing nothing")
            return
        running = True
        #turn on the switch. Not calling turn_on() to avoid calling the start service again
        entity.internal_turn_on()
        _LOGGER.debug("Presence simulation started")

        current_date = datetime.now(timezone.utc)
        if not restart:
            #set attribute on the switch
            await entity.set_start_datetime(datetime.now(hass.config.time_zone))
        #compute the start date that will be used in the query to get the historic of the entities
        minus_delta = current_date + timedelta(-overridden_delta)
        #expand the entitiies, meaning replace the groups with the entities in it
        expanded_entities = await async_expand_entities(overridden_entities)
        await entity.set_entities(expanded_entities)
        _LOGGER.debug("Getting the historic from %s for %s", minus_delta, expanded_entities)
        #dic = history.get_significant_states(hass=hass, start_time=minus_delta, entity_ids=expanded_entities)
        dic = history.get_significant_states(hass=hass, start_time=minus_delta, entity_ids=expanded_entities, significant_changes_only=False)
        _LOGGER.debug("history: %s", dic)
        for entity_id in dic:
            _LOGGER.debug('Entity %s', entity_id)
            #launch an async task by entity_id
            hass.async_create_task(simulate_single_entity(entity_id, dic[entity_id]))

        #launch an async task that will restart the silulation after the delay has passed
        hass.async_create_task(restart_presence_simulation(call))
        _LOGGER.debug("All async tasks launched")

    async def handle_toggle_presence_simulation(call):
        """Toggle the presence simulation"""
        if is_running():
            await handle_stop_presence_simulation(call, restart=False)
        else:
            await handle_presence_simulation(call, restart=False)


    async def restart_presence_simulation(call):
        """Make sure that once _delta_ days is passed, relaunch the presence simulation for another _delta_ days"""
        if call is not None: #if we are here, it is a call of the service, or a restart at the end of a cycle
            overridden_delta = call.data.get("delta", delta)
        else:
            overridden_delta = delta
        _LOGGER.debug("Presence simulation will be relaunched in %i days", overridden_delta)
        #compute the moment the presence simulation will have to be restarted
        start_plus_delta = datetime.now(timezone.utc) + timedelta(overridden_delta)
        while is_running():
            #sleep until the 'delay' is passed
            await asyncio.sleep(interval)
            now = datetime.now(timezone.utc)
            if now > start_plus_delta:
                break

        if is_running():
            _LOGGER.debug("%s has passed, presence simulation is relaunched", overridden_delta)
            #Call top stop needed to avoid the start to do nothing since already running
            await handle_stop_presence_simulation(call, restart=True)
            await handle_presence_simulation(call, restart=True)

    async def simulate_single_entity(entity_id, hist):
        """This method will replay the historic of one entity received in parameter"""
        _LOGGER.debug("Simulate one entity: %s", entity_id)
        for state in hist: #hypothsis: states are ordered chronologically
            _LOGGER.debug("State %s", state.as_dict())
            #_LOGGER.debug("Switch of %s foreseen at %s", entity_id, (state.last_updated+timedelta(delta)).replace(tzinfo=hass.config.time_zone))
            _LOGGER.debug("Switch of %s foreseen at %s", entity_id, state.last_updated+timedelta(delta))
            #get the switch entity
            entity = hass.data[DOMAIN][SWITCH_PLATFORM][SWITCH]
            #await entity.async_add_next_event((state.last_updated+timedelta(delta)).replace(tzinfo=hass.config.time_zone), entity_id, state.state)
            await entity.async_add_next_event(state.last_updated+timedelta(delta), entity_id, state.state)

            #a while with sleeps of _interval_ seconds is used here instead of a big sleep to check regulary the is_running() parameter
            #and therefore stop the task as soon as the service has been stopped
            while is_running():
                minus_delta = datetime.now(timezone.utc) + timedelta(-delta)
                if state.last_updated <= minus_delta:
                    break
                #sleep as long as the event is not in the past
                await asyncio.sleep(interval)
            if not is_running():
                return # exit if state is false
            #call service to turn on/off the light
            await update_entity(entity_id, state)
            #and remove this event from the attribute list of the switch entity
            await entity.async_remove_event(entity_id)

    async def update_entity(entity_id, state):
        """ Switch the entity """
        # get the domain of the entity
        domain = entity_id.split('.')[0]
        #prepare the data of the services
        service_data = {"entity_id": entity_id}
        if domain == "light":
            #if it is a light, checking the brigthness & color
            _LOGGER.debug("Switching light %s to %s", entity_id, state.state)
            if "brightness" in state.attributes:
                _LOGGER.debug("Got attribute brightness: %s", state.attributes["brightness"])
                service_data["brightness"] = state.attributes["brightness"]
            if "rgb_color" in state.attributes:
                service_data["rgb_color"] = state.attributes["rgb_color"]
            await hass.services.async_call("light", "turn_"+state.state, service_data, blocking=False)
        elif domain == "cover":
            #if it is a cover, checking the position
            _LOGGER.debug("Switching Cover %s to %s", entity_id, state.state)
            if "current_tilt_position" in state.attributes:
                #Blocking open/close service if the tilt need to be called at the end
                blocking = True
            else:
                blocking = False
            if state.state == "closed":
                _LOGGER.debug("Closing cover %s", entity_id)
                #await hass.services.async_call("cover", "close_cover", service_data, blocking=blocking)
            elif state.state == "open":
                if "current_position" in state.attributes:
                    service_data["position"] = state.attributes["current_position"]
                    _LOGGER.debug("Changing cover %s position to %s", entity_id, state.attributes["current_position"])
                    #await hass.services.async_call("cover", "set_cover_position", service_data, blocking=blocking)
                    del service_data["position"]
                else: #no position info, just open it
                    _LOGGER.debug("Opening cover %s", entity_id)
                    #await hass.services.async_call("cover", "open_cover", service_data, blocking=blocking)
            if state.state in ["closed", "open"]: #nothing to do if closing or opening. Wait for the status to be 'stabilized'
                if "current_tilt_position" in state.attributes:
                    service_data["tilt_position"] = state.attributes["current_tilt_position"]
                    _LOGGER.debug("Changing cover %s tilt position to %s", entity_id, state.attributes["current_tilt_position"])
                    #await hass.services.async_call("cover", "set_cover_tilt_position", service_data, blocking=False)
                    del service_data["tilt_position"]
        else:
            _LOGGER.debug("Switching entity %s to %s", entity_id, state.state)
            await hass.services.async_call("homeassistant", "turn_"+state.state, service_data, blocking=False)

    def is_running():
        """Returns true if the simulation is running"""
        entity = hass.data[DOMAIN][SWITCH_PLATFORM][SWITCH]
        return entity.is_on


    hass.services.async_register(DOMAIN, "start", handle_presence_simulation)
    hass.services.async_register(DOMAIN, "stop", handle_stop_presence_simulation)
    hass.services.async_register(DOMAIN, "toggle", handle_toggle_presence_simulation)

    return True

async def update_listener(hass, entry):
    """Update listener after an update in the UI"""
    _LOGGER.debug("Updating listener");
    # The OptionsFlow saves data to options.
    if len(entry.options) > 0:
        entry.data = entry.options
        entry.options = {}
        await async_mysetup(hass, [entry.data["entities"]], entry.data["delta"], entry.data["interval"])
