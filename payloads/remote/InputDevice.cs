// evdev/uinput ABI and discovery adapted from samsung-tv-root at 4ad2c4d3b0d0,
// payloads/common/SamsungTvRemoteInputAgent.cs (Unlicense). Discovery here uses
// ioctl identity/capabilities on the opened fd, not an unstable event-node index.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace Theatre.Remote
{
    internal readonly struct InputEvent
    {
        public ushort Type { get; }
        public ushort Code { get; }
        public int Value { get; }

        private InputEvent(ushort type, ushort code, int value)
        {
            Type = type;
            Code = code;
            Value = value;
        }

        public static int Size(int pointerBytes)
        {
            if (pointerBytes != 4 && pointerBytes != 8)
                throw new InvalidDataException("unsupported input_event ABI");
            return pointerBytes * 2 + 8;
        }

        public static InputEvent Parse(byte[] record, int pointerBytes)
        {
            if (record.Length != Size(pointerBytes) || !BitConverter.IsLittleEndian)
                throw new InvalidDataException("invalid input_event record");
            int offset = pointerBytes * 2;
            return new InputEvent(BitConverter.ToUInt16(record, offset),
                BitConverter.ToUInt16(record, offset + 2), BitConverter.ToInt32(record, offset + 4));
        }

        public static byte[] Create(int pointerBytes, ushort type, ushort code, int value)
        {
            var bytes = new byte[Size(pointerBytes)];
            int offset = pointerBytes * 2;
            Buffer.BlockCopy(BitConverter.GetBytes(type), 0, bytes, offset, 2);
            Buffer.BlockCopy(BitConverter.GetBytes(code), 0, bytes, offset + 2, 2);
            Buffer.BlockCopy(BitConverter.GetBytes(value), 0, bytes, offset + 4, 4);
            return bytes;
        }
    }

    internal sealed class RemoteIdentity
    {
        public const string PhysicalName = "Smart Control 2016";
        public const string VirtualName = "theatre-forwarded-remote";
        public string Name { get; }
        public string Node { get; }
        public ushort Bus { get; }
        public ushort Vendor { get; }
        public ushort Product { get; }
        public ushort Version { get; }

        public RemoteIdentity(string name, string node, byte[] id)
        {
            if (id.Length != 8) throw new InvalidDataException("invalid input_id");
            Name = name;
            Node = node;
            Bus = BitConverter.ToUInt16(id, 0);
            Vendor = BitConverter.ToUInt16(id, 2);
            Product = BitConverter.ToUInt16(id, 4);
            Version = BitConverter.ToUInt16(id, 6);
        }

        public bool Matches => Name == PhysicalName && Bus == 0x0005
            && Vendor == 0x04e8 && Product == 0x2075;

        public string Json(int pid, uint uid, uint gid, string mode)
        {
            if (pid <= 0 || uid != 0 || gid != 0)
                throw new InvalidOperationException("input agent requires root identity");
            return JsonSerializer.Serialize(new
            {
                process_id = pid, user_id = uid, group_id = gid, device = Name, node = Node,
                bus = Bus.ToString("x4", CultureInfo.InvariantCulture),
                vendor = Vendor.ToString("x4", CultureInfo.InvariantCulture),
                product = Product.ToString("x4", CultureInfo.InvariantCulture),
                version = Version.ToString("x4", CultureInfo.InvariantCulture), mode
            });
        }
    }

    // Native operations never wait for input/output: fds use O_NONBLOCK.
    // Write is exact-or-error, including EAGAIN, short writes and EINTR.
    internal interface IDeviceIo
    {
        int Open(string path, bool writing);
        byte[] Query(int descriptor, uint request, int length);
        void Control(int descriptor, uint request, int value);
        int Read(int descriptor, byte[] record);
        void Write(int descriptor, byte[] record);
        void Close(int descriptor);
    }

    internal interface ISessionWaiter
    {
        void Wait(int inputDescriptor, int socketDescriptor, int milliseconds);
    }

    internal sealed class NativeIo : IDeviceIo, ISessionWaiter
    {
        internal const uint Grab = 0x40044590;
        internal const uint SetEvent = 0x40045564;
        internal const uint SetKey = 0x40045565;
        internal const uint SetMisc = 0x40045568;
        internal const uint Create = 0x5501;
        internal const uint Destroy = 0x5502;
        internal static uint QueryCode(int number, int length) => 0x80004500u
            | (uint)number | ((uint)length << 16);

        [StructLayout(LayoutKind.Sequential)]
        private struct PollDescriptor
        {
            public int Descriptor;
            public short Events;
            public short ReturnedEvents;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct TimeSpec
        {
            public IntPtr Seconds;
            public IntPtr Nanoseconds;
        }

        [DllImport("libc", SetLastError = true)]
        private static extern int clock_gettime(int clock, out TimeSpec time);

        internal static long BootMilliseconds()
        {
            // Linux uapi CLOCK_BOOTTIME=7, timespec uses native-width longs.
            if (clock_gettime(7, out TimeSpec time) != 0) throw Error("read boot clock");
            return checked(time.Seconds.ToInt64() * 1000 + time.Nanoseconds.ToInt64() / 1000000);
        }

        [DllImport("libc", SetLastError = true)]
        private static extern int open(string path, int flags);
        [DllImport("libc", SetLastError = true)]
        private static extern IntPtr read(int fd, byte[] buffer, UIntPtr count);
        [DllImport("libc", SetLastError = true)]
        private static extern IntPtr write(int fd, byte[] buffer, UIntPtr count);
        [DllImport("libc", SetLastError = true)]
        private static extern int close(int fd);
        [DllImport("libc", SetLastError = true, EntryPoint = "ioctl")]
        private static extern int ioctl_integer(int fd, UIntPtr request, int value);
        [DllImport("libc", SetLastError = true, EntryPoint = "ioctl")]
        private static extern int ioctl_bytes(int fd, UIntPtr request, [Out] byte[] value);
        [DllImport("libc", SetLastError = true)]
        private static extern int poll([In, Out] PollDescriptor[] descriptors, UIntPtr count, int timeout);
        [DllImport("libc")] internal static extern int getpid();
        [DllImport("libc")] internal static extern uint getuid();
        [DllImport("libc")] internal static extern uint getgid();
        [DllImport("libc")] internal static extern uint geteuid();
        [DllImport("libc")] internal static extern uint getegid();

        private static IOException Error(string operation) =>
            new IOException(operation + " failed with errno " + Marshal.GetLastWin32Error());

        public int Open(string path, bool writing)
        {
            // O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW. Never follow an input-node symlink.
            int fd = open(path, OpenFlags(RuntimeInformation.ProcessArchitecture, writing));
            if (fd < 0) throw Error("open device");
            return fd;
        }

        internal static int OpenFlags(Architecture architecture, bool writing)
        {
            // Linux arm/arm64 uapi fcntl.h uses 0100000 for O_NOFOLLOW;
            // x86 uses asm-generic's 00400000. IntPtr.Size cannot distinguish them.
            int noFollow = architecture switch
            {
                Architecture.Arm => 0x8000,
                Architecture.Arm64 => 0x8000,
                Architecture.X86 => 0x20000,
                Architecture.X64 => 0x20000,
                _ => throw new PlatformNotSupportedException("unsupported Linux input ABI")
            };
            return (writing ? 1 : 0) | 0x80000 | 0x800 | noFollow;
        }

        public byte[] Query(int descriptor, uint request, int length)
        {
            var bytes = new byte[length];
            if (ioctl_bytes(descriptor, new UIntPtr(request), bytes) < 0)
                throw Error("query input device");
            return bytes;
        }

        public void Control(int descriptor, uint request, int value)
        {
            if (ioctl_integer(descriptor, new UIntPtr(request), value) != 0)
                throw Error("control input device");
        }

        public int Read(int descriptor, byte[] record)
        {
            long count = read(descriptor, record, new UIntPtr((uint)record.Length)).ToInt64();
            if (count >= 0) return checked((int)count);
            int error = Marshal.GetLastWin32Error();
            if (error == 11 || error == 4) return -1; // EAGAIN / EINTR
            throw Error("read input device");
        }

        public void Write(int descriptor, byte[] record)
        {
            if (write(descriptor, record, new UIntPtr((uint)record.Length)).ToInt64() != record.Length)
                throw Error("write uinput");
        }

        public void Close(int descriptor)
        {
            // Linux releases the fd even on EINTR. Retrying could close a reused fd.
            close(descriptor);
        }

        public void Wait(int inputDescriptor, int socketDescriptor, int milliseconds)
        {
            var descriptors = new[]
            {
                new PollDescriptor { Descriptor = inputDescriptor, Events = 1 },
                new PollDescriptor { Descriptor = socketDescriptor, Events = 1 }
            };
            int count = poll(descriptors, new UIntPtr(2), milliseconds);
            if (count < 0)
            {
                if (Marshal.GetLastWin32Error() == 4) return;
                throw Error("poll input session");
            }
            if ((descriptors[0].ReturnedEvents & (8 | 16 | 32)) != 0)
                throw new IOException("physical input device lost");
            // Let the channel consume EOF; POLLNVAL/POLLERR are always fatal.
            if ((descriptors[1].ReturnedEvents & (8 | 32)) != 0)
                throw new IOException("input connection failed");
        }
    }

    internal sealed class InputDevice : IDisposable
    {
        private readonly IDeviceIo io;
        public RemoteIdentity Identity { get; }
        public byte[] EventBits { get; }
        public byte[] KeyBits { get; }
        public byte[] MiscBits { get; }
        public int PointerBytes { get; }
        public int Descriptor { get; private set; }
        private bool grabbed;
        public bool CleanupFailed { get; private set; }

        private InputDevice(IDeviceIo io, int descriptor, RemoteIdentity identity,
            byte[] events, byte[] keys, byte[] misc, int pointerBytes)
        {
            this.io = io;
            Descriptor = descriptor;
            Identity = identity;
            EventBits = events;
            KeyBits = keys;
            MiscBits = misc;
            PointerBytes = pointerBytes;
        }

        public static bool Has(byte[] bits, int code) =>
            code >= 0 && code / 8 < bits.Length && (bits[code / 8] & (1 << (code % 8))) != 0;

        public static InputDevice Discover(IDeviceIo io, IEnumerable<string> nodes, int pointerBytes)
        {
            InputEvent.Size(pointerBytes);
            InputDevice? selected = null;
            try
            {
                foreach (string node in nodes)
                {
                    // Enumeration is only a hint. All identity reads use this same fd.
                    int descriptor = io.Open(node, false);
                    try
                    {
                        byte[] name = io.Query(descriptor, NativeIo.QueryCode(6, 256), 256);
                        int end = Array.IndexOf(name, (byte)0);
                        if (end < 0) throw new InvalidDataException("unterminated device name");
                        string text = new UTF8Encoding(false, true).GetString(name, 0, end);
                        if (text != RemoteIdentity.PhysicalName) continue;
                        var identity = new RemoteIdentity(text, node,
                            io.Query(descriptor, NativeIo.QueryCode(2, 8), 8));
                        if (!identity.Matches) continue;
                        byte[] events = io.Query(descriptor, NativeIo.QueryCode(0x20, 4), 4);
                        byte[] keys = io.Query(descriptor, NativeIo.QueryCode(0x21, 96), 96);
                        if (!Has(events, 0) || !Has(events, 1) || !Has(keys, 63) || !Has(keys, 560))
                            throw new InvalidDataException("remote is missing required key capabilities");
                        byte[] misc = Has(events, 4)
                            ? io.Query(descriptor, NativeIo.QueryCode(0x24, 1), 1) : new byte[1];
                        if (selected != null) throw new InvalidDataException("ambiguous physical remote");
                        selected = new InputDevice(io, descriptor, identity, events, keys, misc, pointerBytes);
                        descriptor = -1; // ownership transferred only after complete validation
                    }
                    finally { if (descriptor >= 0) io.Close(descriptor); }
                }
                return selected ?? throw new InvalidDataException("matching physical remote not found");
            }
            catch { selected?.Dispose(); throw; }
        }

        public bool TryRead(out byte[] record)
        {
            record = new byte[InputEvent.Size(PointerBytes)];
            int count = io.Read(Descriptor, record);
            if (count == -1) return false;
            if (count != record.Length)
                throw new IOException("lost or truncated physical input event");
            InputEvent.Parse(record, PointerBytes);
            return true;
        }

        public void Grab(Health health)
        {
            health.Check();
            // Avoid replaying pre-grab traffic and inheriting half a key gesture.
            for (int count = 0; ; count++)
            {
                health.Check();
                if (!TryRead(out _)) break;
                if (count >= 255) throw new IOException("remote busy before grab");
            }
            RequireReleased();
            health.Check();
            grabbed = true; // even an exceptional acquisition takes the release path
            io.Control(Descriptor, NativeIo.Grab, 1);
            health.Check();
            RequireReleased();
        }

        private void RequireReleased()
        {
            byte[] held = io.Query(Descriptor, NativeIo.QueryCode(0x18, 96), 96);
            foreach (byte value in held)
                if (value != 0) throw new IOException("remote has held keys during acquisition");
        }

        public UinputForwarder CreateForwarder(Health health) => new UinputForwarder(io, this, health);

        public void Dispose()
        {
            int descriptor = Descriptor;
            Descriptor = -1;
            if (descriptor < 0) return;
            try
            {
                if (grabbed)
                {
                    grabbed = false;
                    try { io.Control(descriptor, NativeIo.Grab, 0); }
                    catch (IOException) { CleanupFailed = true; }
                }
            }
            finally { io.Close(descriptor); }
        }
    }
}
