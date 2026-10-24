using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

namespace Theatre.Remote
{
    internal readonly struct Delivery
    {
        public byte[] Record { get; }
        public bool Forwarded { get; }
        public Delivery(byte[] record, bool forwarded) { Record = record; Forwarded = forwarded; }
    }

    internal sealed class PacketFilter
    {
        private readonly string mode;
        private readonly int pointerBytes;
        private readonly HashSet<int> keys;
        private readonly List<byte[]> packet = new List<byte[]>();
        public const int MaximumEvents = 256;

        public PacketFilter(string mode, int pointerBytes, IEnumerable<int> keys)
        {
            if (mode != "observe" && mode != "forward" && mode != "map")
                throw new InvalidDataException("invalid input mode");
            this.mode = mode;
            this.pointerBytes = pointerBytes;
            this.keys = new HashSet<int>(keys);
        }

        public Delivery[] Add(byte[] record)
        {
            InputEvent input = InputEvent.Parse(record, pointerBytes);
            if (input.Type == 0 && input.Code == 3)
                throw new IOException("SYN_DROPPED: remote state is no longer trustworthy");
            if (packet.Count == MaximumEvents)
                throw new InvalidDataException("input packet exceeds size limit");
            packet.Add(record);
            if (input.Type != 0 || input.Code != 0) return Array.Empty<Delivery>();

            var forward = new bool[packet.Count];
            var scans = new List<int>();
            for (int index = 0; index < packet.Count; index++)
            {
                input = InputEvent.Parse(packet[index], pointerBytes);
                forward[index] = mode != "observe";
                if (mode != "map") continue;
                if (input.Type == 4 && input.Code == 4) scans.Add(index);
                if (input.Type == 1)
                {
                    if (keys.Contains(input.Code))
                    {
                        // Hold scans until their key is known: forwarding the scan
                        // early could leak a suppressed shortcut into native input.
                        forward[index] = false;
                        foreach (int scan in scans) forward[scan] = false;
                    }
                    scans.Clear();
                }
            }
            if (mode == "map" && scans.Count != 0)
                throw new InvalidDataException("scan without a key in input packet");
            var result = new Delivery[packet.Count];
            for (int index = 0; index < packet.Count; index++)
                result[index] = new Delivery(packet[index], forward[index]);
            packet.Clear();
            return result;
        }
    }

    internal static class Agent
    {
        public static int Main(string[] arguments)
        {
            try
            {
                if (arguments.Length != 1)
                    throw new ArgumentException("usage: TheatreInputAgent.dll /absolute/private/config.json");
                if (!BitConverter.IsLittleEndian
                    || NativeIo.getuid() != 0 || NativeIo.getgid() != 0
                    || NativeIo.geteuid() != 0 || NativeIo.getegid() != 0)
                    throw new InvalidOperationException("requires little-endian Linux and root UID/GID");
                using (Configuration config = Configuration.Consume(arguments[0]))
                {
                    var native = new NativeIo();
                    string[] nodes = Directory.GetFiles("/dev/input", "event*");
                    Array.Sort(nodes, StringComparer.Ordinal);
                    using (InputDevice input = InputDevice.Discover(
                        native, nodes, IntPtr.Size, config.Keys, config.RemoteName))
                    {
                        var health = new Health(new MonotonicClock());
                        using (var channel = new AuthenticatedChannel(
                            SocketTransport.Connect(config, health), health, config.Token))
                        {
                            RunSession(input, channel, health, config.Mode, native,
                                NativeIo.getpid(), NativeIo.getuid(), NativeIo.getgid(), config.Keys);
                        }
                    }
                }
                return 0;
            }
            catch (Exception error)
            {
                // Never print exception data: parser errors could contain secrets.
                // This is reached only after all input resources have been released.
                Console.Error.WriteLine("input agent stopped: " + error.GetType().Name);
                return 1;
            }
        }

        internal static void RunSession(InputDevice input, AuthenticatedChannel channel,
            Health health, string mode, ISessionWaiter waiter, int pid, uint uid, uint gid,
            IEnumerable<int> keys)
        {
            UinputForwarder? forwarder = null;
            try
            {
                var filter = new PacketFilter(mode, input.PointerBytes, keys);
                channel.Handshake(input.Identity.Json(pid, uid, gid, mode));
                health.Check();
                if (mode != "observe")
                {
                    if (mode == "forward") health.StartForwardWindow();
                    forwarder = input.CreateForwarder(health);
                    input.Grab(health);
                }
                health.Check();
                channel.Send("READY");
                long sequence = 0;
                while (true)
                {
                    health.Check();
                    channel.PumpHeartbeats();
                    int count = 0;
                    // Even an always-readable evdev fd yields to network health
                    // after 32 records. Each record and write also checks expiry.
                    for (; count < 32; count++)
                    {
                        health.Check();
                        if (!input.TryRead(out byte[] record)) break;
                        health.Check();
                        foreach (Delivery delivery in filter.Add(record))
                        {
                            health.Check();
                            if (delivery.Forwarded) forwarder!.Forward(delivery.Record, health);
                            InputEvent item = InputEvent.Parse(delivery.Record, input.PointerBytes);
                            sequence = checked(sequence + 1);
                            channel.Send(string.Join("\t", "EVENT",
                                sequence.ToString(CultureInfo.InvariantCulture),
                                item.Type.ToString(CultureInfo.InvariantCulture),
                                item.Code.ToString(CultureInfo.InvariantCulture),
                                item.Value.ToString(CultureInfo.InvariantCulture),
                                delivery.Forwarded ? "1" : "0"));
                        }
                    }
                    health.Check();
                    if (count < 32)
                        waiter.Wait(input.Descriptor, channel.Descriptor, health.Remaining(health.Deadline));
                }
            }
            catch (EndOfStreamException)
            {
                // Host closes its accepted socket for normal shutdown.
            }
            finally
            {
                // Release-first is deliberate. Even if virtual key releases fail,
                // kernel fd close has already restored physical Samsung input.
                try { input.Dispose(); }
                finally { forwarder?.Dispose(); }
            }
            if (input.CleanupFailed || (forwarder?.CleanupFailed ?? false))
                throw new IOException("input cleanup reported a native failure");
        }
    }
}
