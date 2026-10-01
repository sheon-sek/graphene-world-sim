import { reachableEndpoint } from "./OpcUaPage";

test("the OPC UA endpoint names the host the page came from, not the bind address", () => {
  expect(reachableEndpoint("opc.tcp://0.0.0.0:4840/graphene/twin", "192.168.1.20")).toBe(
    "opc.tcp://192.168.1.20:4840/graphene/twin",
  );
  expect(reachableEndpoint("opc.tcp://sim.local:4840/graphene/twin", "localhost")).toBe(
    "opc.tcp://sim.local:4840/graphene/twin",
  );
});
