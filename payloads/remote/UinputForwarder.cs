// Legacy uinput_user_dev setup and held-key release adapted from samsung-tv-root
// at 4ad2c4d3b0d0, SamsungTvRemoteInputAgent.cs (Unlicense).
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace Theatre.Remote
{
    internal sealed class UinputForwarder : IDisposable
    {
        private readonly IDeviceIo io;
        private readonly InputDevice source;
        private readonly HashSet<ushort> held = new HashSet<ushort>();
        private int descriptor = -1;
        private bool created;
        public bool CleanupFailed { get; private set; }

        public UinputForwarder(IDeviceIo io, InputDevice source, Health health)
        {
            this.io = io;
            this.source = source;
            try
            {
                health.Check();
                descriptor = io.Open("/dev/uinput", true);
                for (int type = 0; type < 32; type++)
                {
                    health.Check();
                    if (!InputDevice.Has(source.EventBits, type)) continue;
                    // This keyboard remote must not silently lose unsupported axes,
                    // switches or force feedback. Such a device cannot be grabbed.
                    if (type != 0 && type != 1 && type != 4 && type != 20)
                        throw new InvalidDataException("unsupported remote event capability");
                    io.Control(descriptor, NativeIo.SetEvent, type);
                }
                for (int code = 0; code < 768; code++)
                {
                    health.Check();
                    if (InputDevice.Has(source.KeyBits, code))
                        io.Control(descriptor, NativeIo.SetKey, code);
                }
                for (int code = 0; code < 8; code++)
                {
                    health.Check();
                    if (InputDevice.Has(source.MiscBits, code))
                        io.Control(descriptor, NativeIo.SetMisc, code);
                }
                var definition = new byte[1116];
                byte[] name = Encoding.UTF8.GetBytes(RemoteIdentity.VirtualName);
                Buffer.BlockCopy(name, 0, definition, 0, name.Length);
                Buffer.BlockCopy(BitConverter.GetBytes(source.Identity.Bus), 0, definition, 80, 2);
                Buffer.BlockCopy(BitConverter.GetBytes(source.Identity.Vendor), 0, definition, 82, 2);
                Buffer.BlockCopy(BitConverter.GetBytes(source.Identity.Product), 0, definition, 84, 2);
                Buffer.BlockCopy(BitConverter.GetBytes(source.Identity.Version), 0, definition, 86, 2);
                health.Check();
                io.Write(descriptor, definition);
                health.Check();
                // Destroy is attempted even if creation returns an error.
                created = true;
                io.Control(descriptor, NativeIo.Create, 0);
                health.Check();
                if (InputDevice.Has(source.EventBits, 20))
                {
                    // evdev already supplies value=2 repeats. Do not synthesize a
                    // second software-repeat stream in the virtual keyboard.
                    io.Write(descriptor, InputEvent.Create(source.PointerBytes, 20, 0, 0));
                    health.Check();
                    io.Write(descriptor, InputEvent.Create(source.PointerBytes, 20, 1, 0));
                    health.Check();
                }
                io.Write(descriptor, InputEvent.Create(source.PointerBytes, 0, 0, 0));
                health.Check();
            }
            catch { Dispose(); throw; }
        }

        public void Forward(byte[] record, Health health)
        {
            health.Check();
            InputEvent input = InputEvent.Parse(record, source.PointerBytes);
            if (!InputDevice.Has(source.EventBits, input.Type)
                || (input.Type == 1 && !InputDevice.Has(source.KeyBits, input.Code))
                || (input.Type == 4 && !InputDevice.Has(source.MiscBits, input.Code)))
                throw new InvalidDataException("event outside remote capabilities");
            if (input.Type == 1 && input.Value != 0) held.Add(input.Code);
            // Track a possibly accepted down even if write reports an error.
            io.Write(descriptor, record);
            if (input.Type == 1 && input.Value == 0) held.Remove(input.Code);
            health.Check();
        }

        public void Dispose()
        {
            int current = descriptor;
            descriptor = -1;
            if (current < 0) return;
            try
            {
                // No retry/wait here: release of the physical fd precedes this
                // cleanup. Continue with every remaining key even after a failure.
                foreach (ushort code in held)
                {
                    try { io.Write(current, InputEvent.Create(source.PointerBytes, 1, code, 0)); }
                    catch (IOException) { CleanupFailed = true; }
                }
                if (held.Count > 0)
                {
                    try { io.Write(current, InputEvent.Create(source.PointerBytes, 0, 0, 0)); }
                    catch (IOException) { CleanupFailed = true; }
                }
                held.Clear();
            }
            finally
            {
                try
                {
                    if (created)
                    {
                        created = false;
                        try { io.Control(current, NativeIo.Destroy, 0); }
                        catch (IOException) { CleanupFailed = true; }
                    }
                }
                finally { io.Close(current); }
            }
        }
    }
}
